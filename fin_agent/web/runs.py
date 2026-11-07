"""Transactional Web lifecycle. Execution belongs to a worker, never an HTTP stream."""
import json
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select

from fin_agent.web.auth import digest
from fin_agent.web.models import ChatSession, Conversation, Message, Run, RunEvent, Span, WorkerState, identifier

ACTIVE = ('queued', 'running')
TERMINAL = ('completed', 'failed', 'interrupted')


def run_view(run):
    return {key: getattr(run, key) for key in ('id', 'session_id', 'status', 'received_at',
        'started_at', 'finished_at', 'last_seq', 'first_content_at', 'retry_of', 'input_message_id')}


def event_view(event):
    return {'v': 1, 'run_id': event.run_id, 'seq': event.seq, 'type': event.type,
            'at': datetime.fromtimestamp(event.at, timezone.utc).isoformat(), 'data': event.data}


def append(db, run, kind, data):
    run.last_seq += 1
    db.add(RunEvent(run_id=run.id, seq=run.last_seq, type=kind, data=data))


def fail_run(db, run, *, status='failed', code='execution_failed', error=None):
    if status not in ('failed', 'interrupted'):
        raise ValueError('Invalid failure state')
    run.status, run.finished_at = status, time.time()
    if error:
        db.add(Span(id=identifier(), run_id=run.id, kind='execution',
                    started={'code': code}, finished={'status': status, 'error': error}))
    for span in db.scalars(select(Span).where(Span.run_id == run.id, Span.finished.is_(None))):
        span.finished = {'status': status, 'duration_ms': None, 'code': code}
        if span.kind == 'tool':
            append(db, run, 'tool.finished', {'span_id': span.id, 'name': span.started['name'],
                                            'status': status, 'duration_ms': None})
    for message in db.scalars(select(Message).where(Message.run_id == run.id, Message.status == 'draft')):
        message.status = status
    append(db, run, 'run.' + status, {'code': code, 'message': '本次分析未完成，请稍后重试。'})


class Runs:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    @staticmethod
    def owned_session(db, session_id, user_id):
        session = db.scalar(select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id))
        if session is None:
            raise HTTPException(404, '会话不存在')
        return session

    @staticmethod
    def owned_run(db, run_id, user_id):
        run = db.scalar(select(Run).where(Run.id == run_id, Run.user_id == user_id))
        if run is None:
            raise HTTPException(404, '运行不存在')
        return run

    def submit(self, session_id, user_id, question, key, *, retry_of=None):
        request_hash = digest(json.dumps(['retry', retry_of] if retry_of else [session_id, question], ensure_ascii=False))
        from fin_agent.agent.redaction import configured_redactor
        question = configured_redactor().text(question)
        with self.database.transaction(immediate=True) as db:
            session = self.owned_session(db, session_id, user_id)
            previous = db.scalar(select(Run).where(Run.user_id == user_id, Run.key == key))
            if previous:
                if previous.request_hash != request_hash:
                    raise HTTPException(409, '提交标识已用于不同的问题')
                return run_view(previous)
            original = None
            if retry_of:
                original = self.owned_run(db, retry_of, user_id)
                latest = db.scalar(select(Run).where(Run.session_id == session_id).order_by(Run.received_at.desc(), Run.id.desc()).limit(1))
                if original.session_id != session_id or original.status not in ('failed', 'interrupted') or latest.id != original.id:
                    raise HTTPException(409, '只能重试当前会话最近一次失败或中断的分析')
            count = lambda *conditions: db.scalar(select(func.count()).select_from(Run).where(*conditions))
            if count(Run.session_id == session_id, Run.status.in_(ACTIVE)):
                raise HTTPException(409, '当前会话已有进行中的问题')
            if count(Run.user_id == user_id, Run.status.in_(ACTIVE)) >= self.settings.user_run_limit:
                raise HTTPException(429, '你的进行中任务较多，请等待完成')
            if count(Run.status == 'queued') >= self.settings.queue_limit:
                raise HTTPException(429, '队列已满，请稍后重试')
            executor = db.get(WorkerState, 1)
            run = Run(id=identifier(), user_id=user_id, session_id=session_id, input=question,
                      key=key, request_hash=request_hash, status='queued', last_seq=0, retry_of=retry_of,
                      input_message_id=original.input_message_id if original else identifier(),
                      owner=executor.epoch if executor else None)
            db.add(run)
            db.flush()
            if original is None:
                db.add(Message(id=run.input_message_id, session_id=session_id, run_id=run.id,
                               role='user', content=question, status='completed'))
            session.updated_at = time.time()
            append(db, run, 'run.queued', {})
            return run_view(run)

    def retry(self, run_id, user_id, key):
        with self.database.transaction() as db:
            original = self.owned_run(db, run_id, user_id)
            session_id, question = original.session_id, original.input
        return self.submit(session_id, user_id, question, key, retry_of=run_id)

    def snapshot(self, session_id, user_id):
        with self.database.transaction() as db:
            # Begin a real read transaction so messages and event cursor share a snapshot.
            db.connection().exec_driver_sql('BEGIN')
            self.owned_session(db, session_id, user_id)
            messages = db.scalars(select(Message).where(Message.session_id == session_id)
                                  .order_by(Message.created_at, Message.id)).all()
            runs = db.scalars(select(Run).where(Run.session_id == session_id).order_by(Run.received_at)).all()
            tools = db.scalars(select(Span).join(Run, Span.run_id == Run.id).where(
                Run.session_id == session_id, Span.kind == 'tool')).all()
            executor = db.get(WorkerState, 1)
            return {'messages': [{k: getattr(m, k) for k in ('id', 'run_id', 'role', 'phase', 'content', 'status')}
                                 for m in messages], 'runs': [run_view(r) for r in runs],
                    'tools': [{'span_id': s.id, 'run_id': s.run_id, 'name': s.started['name'],
                               'status': (s.finished or {}).get('status', 'running'),
                               'duration_ms': (s.finished or {}).get('duration_ms')} for s in tools],
                    'executor': {'available': bool(executor and executor.active and executor.heartbeat_at > time.time() - 15),
                                 'heartbeat_at': executor.heartbeat_at if executor else None}}

    def get(self, run_id, user_id):
        with self.database.transaction() as db:
            return run_view(self.owned_run(db, run_id, user_id))

    def executor_status(self):
        with self.database.transaction() as db:
            executor = db.get(WorkerState, 1)
            return {'available': bool(executor and executor.active and executor.heartbeat_at > time.time() - 15),
                    'heartbeat_at': executor.heartbeat_at if executor else None}

    def events(self, run_id, user_id, after):
        with self.database.transaction() as db:
            db.connection().exec_driver_sql('BEGIN')
            run = self.owned_run(db, run_id, user_id)
            events = db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after)
                                .order_by(RunEvent.seq).limit(100)).all()
            return [event_view(e) for e in events], run_view(run)

    def claim(self, owner):
        with self.database.transaction(immediate=True) as db:
            executor = db.get(WorkerState, 1)
            if executor and (executor.epoch != owner or not executor.active):
                raise RuntimeError('Executor no longer owns the lease')
            run = db.scalar(select(Run).where(Run.status == 'queued').order_by(Run.received_at, Run.id).limit(1))
            if not run:
                return None
            run.owner, run.status, run.started_at = owner, 'running', time.time()
            context = db.get(Conversation, run.session_id)
            append(db, run, 'run.started', {})
            return {'run_id': run.id, 'input': run.input, 'history': context.history if context else []}

    def register_process(self, run_id, owner, pid, created_at):
        with self.database.transaction(immediate=True) as db:
            run = db.get(Run, run_id)
            executor = db.get(WorkerState, 1)
            if not run or run.status != 'running' or run.owner != owner or not executor or executor.epoch != owner:
                raise RuntimeError('Execution ownership changed before subprocess admission')
            run.child_pid, run.child_created_at = pid, created_at

    def clear_process(self, run_id, owner):
        with self.database.transaction(immediate=True) as db:
            run = db.get(Run, run_id)
            if run and run.owner == owner:
                run.child_pid = run.child_created_at = None

    def record(self, run_id, owner, event):
        with self.database.transaction(immediate=True) as db:
            run = db.get(Run, run_id)
            executor = db.get(WorkerState, 1)
            if run is None or run.status != 'running' or run.owner != owner or (executor and executor.epoch != owner):
                return False
            kind = event['type']
            if kind.startswith('model.') or kind.startswith('tool.'):
                span = db.get(Span, event['span_id'])
                if kind.endswith('.started'):
                    span = Span(id=event['span_id'], run_id=run.id, kind=kind.split('.')[0], started=event)
                    db.add(span)
                elif span:
                    if kind.endswith('.first_chunk'):
                        span.first_chunk = event
                    else:
                        span.finished = event
                if kind.startswith('tool.'):
                    append(db, run, kind, {k: event[k] for k in ('span_id', 'name', 'status', 'duration_ms') if k in event})
            elif kind == 'message.interim':
                message = db.get(Message, event['message_id'])
                if message:
                    message.phase = 'interim'
                    append(db, run, kind, {'message_id': message.id})
            elif kind == 'message.delta':
                message = db.get(Message, event['message_id'])
                if message is None:
                    message = Message(id=event['message_id'], session_id=run.session_id, run_id=run.id,
                                      role='assistant', content='', status='draft')
                    db.add(message)
                message.content += event['text']
                if run.first_content_at is None:
                    run.first_content_at = time.time()
                append(db, run, kind, {'message_id': message.id, 'text': event['text']})
            elif kind == 'execution.completed':
                message = db.get(Message, event['message_id'])
                if message is None:
                    message = Message(id=event['message_id'], session_id=run.session_id, run_id=run.id,
                                      role='assistant')
                    db.add(message)
                message.content, message.phase, message.status = event['text'], 'final', 'completed'
                context = db.get(Conversation, run.session_id)
                if context:
                    context.history = event['history']
                else:
                    db.add(Conversation(session_id=run.session_id, history=event['history']))
                run.status, run.finished_at = 'completed', time.time()
                append(db, run, 'message.completed', {'message_id': message.id, 'text': message.content})
                append(db, run, 'run.completed', {})
                for previous in db.scalars(select(Message).where(Message.run_id == run.id, Message.status == 'draft')):
                    previous.status = 'completed'
            elif kind == 'execution.failed':
                fail_run(db, run, status=event.get('status', 'failed'),
                         code=event.get('code', 'execution_failed'), error=event.get('error'))
            return True
