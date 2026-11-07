"""Offline restore admission and observable API-process presence."""
import os
import json
from pathlib import Path
from contextlib import contextmanager

import psutil
from sqlalchemy import select

from fin_agent.agent.trace_store import file_lock
from fin_agent.web.models import ServiceInstance, identifier


def admission_lock(database):
    return file_lock(str(database.path) + '.admission.lock')


@contextmanager
def api_presence(database):
    # The lifetime lock is visible across containers sharing the data volume.
    # PID inspection alone only works inside a single PID namespace.
    identity = identifier()
    directory = Path(str(database.path) + '.services')
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / (identity + '.lock'), blocking=False):
        with _api_presence(database, identity, directory):
            yield


@contextmanager
def _api_presence(database, identity, directory):
    marker = directory / (identity + '.json')
    try:
        with admission_lock(database), database.transaction(immediate=True) as db:
            database.check_schema()
            db.add(ServiceInstance(id=identity, pid=os.getpid(), created_at=psutil.Process().create_time()))
            with marker.open('x') as output:
                json.dump({'pid': os.getpid(), 'created_at': psutil.Process().create_time()}, output)
                output.flush()
                os.fsync(output.fileno())
    except Exception:
        marker.unlink(missing_ok=True)
        raise
    try:
        yield
    finally:
        try:
            with database.transaction(immediate=True) as db:
                instance = db.get(ServiceInstance, identity)
                if instance:
                    db.delete(instance)
        finally:
            marker.unlink(missing_ok=True)


def require_offline(database):
    directory = Path(str(database.path) + '.services')
    for lock in directory.glob('*.lock'):
        try:
            with file_lock(lock, blocking=False):
                pass
        except OSError as exc:
            raise RuntimeError('Stop all Web API processes before restoring the database') from exc
    # Compatibility with presence markers written before the lifetime-lock change.
    for marker in directory.glob('*.json'):
        if marker.with_suffix('.lock').exists():
            continue
        instance = json.loads(marker.read_text())
        try:
            process = psutil.Process(instance['pid'])
            if process.create_time() == instance['created_at'] and process.status() != psutil.STATUS_ZOMBIE:
                raise RuntimeError('Stop all Web API processes before restoring the database')
        except psutil.NoSuchProcess:
            pass
