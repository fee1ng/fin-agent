import time

from sqlalchemy import delete, select

from fin_agent.web.deletions import flush_deletions
from fin_agent.web.models import AuthSession, Run, RunEvent, Span
from fin_agent.web.runs import TERMINAL


def cleanup(database, settings, *, now=None):
    now = time.time() if now is None else now
    flush_deletions(database)
    traces = events = 0
    with database.transaction(immediate=True) as db:
        old_traces = db.scalars(select(Run).where(Run.status.in_(TERMINAL), Run.trace_purged_at.is_(None),
            Run.finished_at < now - settings.trace_days * 86400).limit(500)).all()
        for run in old_traces:
            spans = db.scalars(select(Span).where(Span.run_id == run.id)).all()
            run.trace_summary = {'model_calls': sum(s.kind == 'model' for s in spans),
                'tool_calls': sum(s.kind == 'tool' for s in spans),
                'failed_tools': sum(s.kind == 'tool' and (s.finished or {}).get('status') in ('failed', 'interrupted') for s in spans)}
            db.execute(delete(Span).where(Span.run_id == run.id))
            run.trace_purged_at = now
            traces += 1
        old_events = db.scalars(select(Run).where(Run.status.in_(TERMINAL), Run.events_purged_at.is_(None),
            Run.finished_at < now - settings.event_hours * 3600).limit(500)).all()
        for run in old_events:
            db.execute(delete(RunEvent).where(RunEvent.run_id == run.id))
            run.events_purged_at = now
            events += 1
        db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    return {'trace_runs_purged': traces, 'event_runs_purged': events}
