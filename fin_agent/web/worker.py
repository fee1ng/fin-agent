"""A separate supervisor owns bounded subprocesses; children never write SQLite."""
import contextlib
import json
import multiprocessing
import os
import queue
import time
import uuid
from dataclasses import dataclass

from fin_agent.agent.trace_store import file_lock
from fin_agent.web.database import Database
from fin_agent.web.filtering import TextFilter, visible
from fin_agent.web.runs import Runs
from fin_agent.web.recovery import begin_epoch, end_epoch, heartbeat
from fin_agent.web.processes import terminate_identity


def default_factory(emit):
    from fin_agent.agent.web_observer import create_web_agent
    return create_web_agent(emit)


def child_main(channel, gate, job, factory):
    # A crash between spawn and identity registration cannot start an untracked tool.
    if not gate.wait(timeout=15):
        channel.close()
        return
    # No unfiltered library/debug output is permitted to enter supervisor logs.
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        from fin_agent.agent.redaction import configured_redactor
        redactor = configured_redactor()
        message_id = uuid.uuid4().hex
        filtering = TextFilter(redactor)
        pending, flushed_at = '', time.monotonic()

        def send(event):
            payload = json.dumps(event, ensure_ascii=False)
            if len(payload.encode()) > 2_000_000:
                raise ValueError('Execution event exceeds the configured transport limit')
            channel.put(payload, timeout=15)

        def flush(final=False):
            nonlocal pending, flushed_at
            if final:
                pending += filtering.feed('', final=True)
            if pending:
                send({'type': 'message.delta', 'message_id': message_id, 'text': pending})
            pending, flushed_at = '', time.monotonic()

        def observe(event):
            # LangGraph callbacks may run ahead on a model thread. They must not
            # mutate the text consumer's message or buffering state.
            send(redactor.clean(event))

        try:
            agent = factory(observe)
            if job['history']:
                agent.history = job['history']
            answer = None
            for event in agent.stream_chat(job['input']):
                if event['type'] in ('content', 'answer') and event.get('message_id') and event['message_id'] != message_id:
                    flush(final=True)
                    message_id, filtering = event['message_id'], TextFilter(redactor)
                if event['type'] == 'content':
                    pending += filtering.feed(str(event.get('content', '')))
                    if len(pending) >= 4096 or time.monotonic() - flushed_at >= .05:
                        flush()
                elif event['type'] == 'answer':
                    answer = visible(event.get('content', ''), redactor)
                elif event['type'] == 'tool_call':
                    flush(final=True)
                    send({'type': 'message.interim', 'message_id': message_id})
                elif event['type'] == 'error':
                    raise RuntimeError('Agent did not complete')
                # thinking/raw tool argument fragments are intentionally discarded.
            flush(final=True)
            if answer is None:
                raise RuntimeError('Agent ended without a final answer')
            history = redactor.clean(agent.history)
            for message in history:
                if message.get('role') == 'assistant':
                    message['content'] = visible(message.get('content', ''), redactor)
            send({'type': 'execution.completed', 'message_id': message_id, 'text': answer, 'history': history})
        except Exception as exc:
            send({'type': 'execution.failed', 'code': 'agent_failed',
                  'error': {'type': type(exc).__name__, 'message': redactor.text(str(exc))[:2000]}})
        finally:
            # Drain the feeder before exiting so the supervisor sees the terminal fact.
            channel.close()
            channel.join_thread()


@dataclass
class Child:
    process: object
    channel: object
    started: float
    # Linux spawn must rebuild this semaphore before the parent releases its
    # last reference; retain it for the child's lifetime, like the Queue.
    gate: object
    stopped: bool = False


class Worker:
    def __init__(self, settings, *, factory=default_factory):
        self.settings, self.factory = settings, factory
        self.database = Database(settings.database)
        self.database.check_schema()
        self.runs = Runs(self.database, settings)
        self.owner = uuid.uuid4().hex
        self.children = {}
        self.context = multiprocessing.get_context('spawn')
        self.last_heartbeat = 0
        self.last_maintenance = 0

    def __enter__(self):
        self.lock = file_lock(str(self.database.path) + '.worker.lock', blocking=False)
        try:
            self.lock.__enter__()
        except Exception:
            self.database.close()
            raise
        try:
            from fin_agent.web.services import admission_lock
            with admission_lock(self.database):
                begin_epoch(self.database, self.owner)
        except Exception:
            self.lock.__exit__(None, None, None)
            self.database.close()
            raise
        return self

    def stop_child(self, child):
        if child.stopped:
            return
        if child.process.is_alive():
            import psutil
            try:
                terminate_identity(child.process.pid, psutil.Process(child.process.pid).create_time())
            except psutil.NoSuchProcess:
                pass
        child.process.join(timeout=3)
        if child.process.is_alive():
            raise RuntimeError('Execution subprocess did not stop')
        child.channel.close()
        child.channel.cancel_join_thread()
        child.process.close()
        child.stopped = True

    def __exit__(self, *args):
        failed = False
        try:
            for run_id, child in list(self.children.items()):
                try:
                    self.stop_child(child)
                    self.runs.clear_process(run_id, self.owner)
                    self.runs.record(run_id, self.owner, {'type': 'execution.failed',
                        'status': 'interrupted', 'code': 'worker_stopped'})
                except Exception:
                    # A broken database must not prevent stopping the other child.
                    failed = True
            self.children.clear()
            end_epoch(self.database, self.owner)
        finally:
            self.database.close()
            self.lock.__exit__(*args)
        if failed and not args[0]:
            raise RuntimeError('Worker cleanup did not settle every run')

    def tick(self):
        if time.monotonic() - self.last_heartbeat >= 1:
            heartbeat(self.database, self.owner)
            self.last_heartbeat = time.monotonic()
        if time.monotonic() - self.last_maintenance >= 3600:
            from fin_agent.web.maintenance import cleanup
            from fin_agent.web.backups import prune_backups
            cleanup(self.database, self.settings)
            prune_backups(self.database, self.database.path.parent / 'backups', self.settings.backup_days)
            self.last_maintenance = time.monotonic()
        for run_id, child in list(self.children.items()):
            terminal = False
            # Bound each pass so a chatty model cannot starve the other run.
            for _ in range(100):
                try:
                    event = json.loads(child.channel.get_nowait())
                except queue.Empty:
                    break
                self.runs.record(run_id, self.owner, event)
                if event['type'] in ('execution.completed', 'execution.failed'):
                    terminal = True
                    break
            expired = time.monotonic() - child.started > self.settings.run_timeout
            if terminal or expired or not child.process.is_alive():
                # Queue feeder shutdown is complete before a normal child exits;
                # however a bounded batch may leave buffered events for the next pass.
                if not terminal and not expired and not child.channel.empty():
                    continue
                self.stop_child(child)
                self.runs.clear_process(run_id, self.owner)
                if not terminal:
                    self.runs.record(run_id, self.owner, {'type': 'execution.failed',
                        'code': 'timeout' if expired else 'process_exited'})
                del self.children[run_id]
        while len(self.children) < self.settings.concurrency:
            job = self.runs.claim(self.owner)
            if job is None:
                break
            channel = self.context.Queue(maxsize=32)
            gate = self.context.Event()
            process = self.context.Process(target=child_main, args=(channel, gate, job, self.factory))
            try:
                process.start()
                child = Child(process, channel, time.monotonic(), gate)
                self.children[job['run_id']] = child
                import psutil
                self.runs.register_process(job['run_id'], self.owner, process.pid, psutil.Process(process.pid).create_time())
                gate.set()
            except Exception:
                if job['run_id'] in self.children:
                    self.stop_child(self.children.pop(job['run_id']))
                else:
                    channel.close()
                self.runs.record(job['run_id'], self.owner, {'type': 'execution.failed', 'code': 'process_start_failed'})
                raise

    def serve(self):
        with self:
            while True:
                self.tick()
                time.sleep(.05)
