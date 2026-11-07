"""Ownership transitions are serialized by the worker's operating-system lock."""
import os
import time

from sqlalchemy import select

from fin_agent.web.models import Run, WorkerState
from fin_agent.web.processes import terminate_identity
from fin_agent.web.runs import ACTIVE, fail_run


def begin_epoch(database, epoch):
    with database.transaction() as db:
        identities = [(r.child_pid, r.child_created_at) for r in db.scalars(
            select(Run).where(Run.child_pid.is_not(None)))]
    # No new worker can claim work until all old registered processes have stopped.
    for pid, created in identities:
        terminate_identity(pid, created)
    with database.transaction(immediate=True) as db:
        state = db.get(WorkerState, 1)
        existed = state is not None
        for run in db.scalars(select(Run).where(Run.status.in_(ACTIVE))):
            if existed or run.status == 'running':
                fail_run(db, run, status='interrupted', code='worker_restarted')
            else:
                # Initial installation: questions accepted before the first worker
                # are new work, not an automatic replay of an earlier execution.
                run.owner = epoch
        for run in db.scalars(select(Run).where(Run.child_pid.is_not(None))):
            run.child_pid = run.child_created_at = None
        if state is None:
            state = WorkerState(id=1)
            db.add(state)
        state.epoch, state.pid = epoch, os.getpid()
        state.active, state.heartbeat_at = True, time.time()


def heartbeat(database, epoch):
    with database.transaction(immediate=True) as db:
        state = db.get(WorkerState, 1)
        if not state or state.epoch != epoch or not state.active:
            raise RuntimeError('Worker ownership was revoked')
        state.heartbeat_at = time.time()


def end_epoch(database, epoch):
    with database.transaction(immediate=True) as db:
        state = db.get(WorkerState, 1)
        if state and state.epoch == epoch:
            for run in db.scalars(select(Run).where(Run.owner == epoch, Run.status.in_(ACTIVE))):
                fail_run(db, run, status='interrupted', code='worker_stopped')
            state.active, state.heartbeat_at = False, time.time()
