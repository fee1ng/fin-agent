"""Administrator-only projections over persisted runs and real call boundaries."""
import math
import time
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query
from sqlalchemy import exists, func, select

from fin_agent.web.models import ChatSession, Message, Run, Span, User
from fin_agent.web.runs import run_view


def metric(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def milliseconds(start, end):
    return metric((end - start) * 1000) if start is not None and end is not None else None


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def timing(run):
    return {'queue_ms': milliseconds(run.received_at, run.started_at),
            'execution_ms': milliseconds(run.started_at, run.finished_at),
            'total_ms': milliseconds(run.received_at, run.finished_at),
            'first_content_ms': milliseconds(run.received_at, run.first_content_at),
            'execution_elapsed_ms': milliseconds(run.started_at, run.finished_at or time.time())}


def span_view(span, run):
    started, finished, first = span.started, span.finished or {}, span.first_chunk or {}
    start = timestamp(started.get('at'))
    if start is None and span.kind == 'execution':
        start = run.finished_at
    return {'id': span.id, 'kind': span.kind, 'name': started.get('name') or started.get('model') or started.get('code') or span.kind,
            'parent_id': started.get('parent_id'), 'tool_call_id': started.get('tool_call_id'),
            'started_at': start, 'ended_at': timestamp(finished.get('at')),
            'offset_ms': milliseconds(run.started_at, start), 'duration_ms': metric(finished.get('duration_ms')),
            'status': finished.get('status', 'running' if run.status == 'running' else 'unknown'),
            'first_chunk_ms': metric(first.get('duration_ms')), 'first_chunk_kind': first.get('kind'),
            'usage': finished.get('usage'), 'arguments': started.get('arguments'),
            'summary': finished.get('summary'), 'error': finished.get('error'), 'code': finished.get('code')}


def install_admin_routes(app):
    database = app.state.database
    admin = Annotated[User, Depends(app.state.admin_user)]

    def clean(value):
        # Defence in depth for old records created before a key was configured.
        from fin_agent.agent.redaction import configured_redactor
        return configured_redactor().clean(value)

    @app.get('/api/v1/admin/users')
    def users(user: admin):
        with database.transaction() as db:
            return {'users': [{'id': row.id, 'username': row.username, 'disabled': row.disabled}
                             for row in db.scalars(select(User).order_by(User.username))]}

    @app.get('/api/v1/admin/runs')
    def runs(user: admin, user_id: str | None = None, username: str | None = None,
             session_id: str | None = None, run_id: str | None = None, tool: str | None = None,
             status: Literal['queued', 'running', 'completed', 'failed', 'interrupted'] | None = None,
             since: datetime | None = None, until: datetime | None = None,
             offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 25):
        for bound in (since, until):
            if bound is not None and bound.tzinfo is None:
                raise HTTPException(422, '时间筛选需要包含时区')
        if since and until and since > until:
            raise HTTPException(422, '开始时间不能晚于结束时间')
        conditions = []
        for column, value in ((Run.user_id, user_id), (Run.session_id, session_id), (Run.id, run_id), (Run.status, status)):
            if value:
                conditions.append(column == value)
        if username:
            conditions.append(User.username == username.strip().casefold())
        if since:
            conditions.append(Run.received_at >= since.timestamp())
        if until:
            conditions.append(Run.received_at <= until.timestamp())
        if tool:
            conditions.append(exists(select(Span.id).where(Span.run_id == Run.id, Span.kind == 'tool',
                                                          Span.started['name'].as_string() == tool)))
        with database.transaction() as db:
            db.connection().exec_driver_sql('BEGIN')
            total = db.scalar(select(func.count()).select_from(Run).join(User, Run.user_id == User.id).where(*conditions))
            rows = db.execute(select(Run, User.username, ChatSession.title).join(User, Run.user_id == User.id)
                .join(ChatSession, Run.session_id == ChatSession.id).where(*conditions)
                .order_by(Run.received_at.desc(), Run.id.desc()).offset(offset).limit(limit)).all()
            ids = [r.id for r, _, _ in rows]
            calls = db.execute(select(Span.run_id, Span.kind, Span.finished['status'].as_string(),
                                      Span.finished['duration_ms'].as_float()).where(Span.run_id.in_(ids))).all() if ids else []
            result = []
            for run, name, title in rows:
                tools = [(state, duration) for rid, kind, state, duration in calls if rid == run.id and kind == 'tool']
                durations = [duration for _, duration in tools if metric(duration) is not None]
                result.append({**run_view(run), **timing(run), 'user_id': run.user_id, 'username': name,
                    'title': title, 'input_preview': clean(run.input)[:120], 'engine': 'langgraph',
                    'tool_calls': len(tools), 'failed_tools': sum(state in ('failed', 'interrupted') for state, _ in tools),
                    'slowest_tool_ms': max(durations, default=None)})
                if run.trace_purged_at is not None:
                    result[-1].update(run.trace_summary or {})
                    result[-1]['trace_state'] = 'expired'
            return clean({'runs': result, 'total': total, 'offset': offset, 'limit': limit})

    @app.get('/api/v1/admin/runs/{run_id}')
    def detail(run_id: str, user: admin):
        with database.transaction() as db:
            db.connection().exec_driver_sql('BEGIN')
            run = db.get(Run, run_id)
            if run is None:
                raise HTTPException(404, '运行不存在')
            owner = db.get(User, run.user_id)
            session = db.get(ChatSession, run.session_id)
            spans = [span_view(span, run) for span in db.scalars(select(Span).where(Span.run_id == run.id))]
            spans.sort(key=lambda s: (s['started_at'] is None, s['started_at'] or 0, s['id']))
            models = [s for s in spans if s['kind'] == 'model']
            usage = {key: (sum(values) if models and all(v is not None for v in values) else None)
                     for key in ('input', 'output', 'total')
                     for values in [[metric((s['usage'] or {}).get(key)) for s in models]]}
            tools = [s for s in spans if s['kind'] == 'tool']
            answer = db.scalars(select(Message).where(Message.run_id == run.id, Message.role == 'assistant')
                                 .order_by(Message.created_at, Message.id)).all()
            summary = run.trace_summary or {}
            return clean({'run': {**run_view(run), **timing(run), 'user_id': run.user_id,
                                 'username': owner.username, 'title': session.title, 'engine': 'langgraph'},
                          'input': {'message_id': run.input_message_id, 'text': run.input},
                          'answers': [{'id': m.id, 'content': m.content, 'phase': m.phase, 'status': m.status} for m in answer],
                          'spans': spans, 'usage': usage, 'model_calls': summary.get('model_calls', len(models)),
                          'tool_calls': summary.get('tool_calls', len(tools)),
                          'failed_tools': summary.get('failed_tools', sum(s['status'] in ('failed', 'interrupted') for s in tools)),
                          'slowest_span_id': max((s for s in spans if s['duration_ms'] is not None),
                                                 key=lambda s: s['duration_ms'], default={}).get('id'),
                          'trace_state': 'expired' if run.trace_purged_at is not None else 'available' if spans else 'unavailable',
                          'coverage': '记录应用层实际调用边界；SDK 内部隐藏重试包含在请求耗时中，无法逐次拆分。',
                          'browser_first_render_ms': None})
