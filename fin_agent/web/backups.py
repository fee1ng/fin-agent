"""Consistent SQLite backups and offline restore with deletion replay."""
import hashlib
import json
import os
import sqlite3
import time
import math
import uuid
from contextlib import closing
from pathlib import Path

from sqlalchemy import delete, select

from fin_agent.agent.trace_store import file_lock
from fin_agent.web.database import Database
from fin_agent.web.deletions import DeletionJournal, flush_deletions
from fin_agent.web.models import AuthSession, ChatSession, DeletedSession, Run, ServiceInstance, WorkerState
from fin_agent.web.processes import terminate_identity
from fin_agent.web.runs import ACTIVE, fail_run
from fin_agent.web.services import admission_lock, require_offline

FORMAT = 'fin-agent-web-backup-v1'


def checksum(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def database_id(database):
    return hashlib.sha256(str(database.path).encode()).hexdigest()


def sqlite_copy(source, destination):
    with closing(sqlite3.connect(Path(source).as_uri() + '?mode=ro', uri=True, timeout=10)) as src, \
         closing(sqlite3.connect(destination)) as target:
        src.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite backup integrity check failed')


def backup_file(database, destination):
    flush_deletions(database)
    target = Path(destination).resolve()
    metadata = Path(str(target) + '.json')
    if target == database.path or target.exists() or metadata.exists():
        raise FileExistsError('Backup destination must be a new file')
    target.parent.mkdir(parents=True, exist_ok=True)
    created = meta_created = False
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        created = True
        sqlite_copy(database.path, target)
        with target.open('rb+') as saved:
            os.fsync(saved.fileno())
        manifest = {'format': FORMAT, 'database_id': database_id(database), 'filename': target.name,
                    'sha256': checksum(target), 'created_at': time.time(),
                    'deletion_records': len(DeletionJournal(database.path).read())}
        with metadata.open('x', encoding='utf-8') as output:
            meta_created = True
            json.dump(manifest, output)
            output.flush()
            os.fsync(output.fileno())
        return {'path': str(target), **manifest}
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        if meta_created:
            metadata.unlink(missing_ok=True)
        raise


def delete_mode(database):
    # SQLite itself checkpoints and removes its WAL/SHM files before replacement.
    with database.engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.commit()
        mode = connection.exec_driver_sql('PRAGMA journal_mode=DELETE').scalar()
        if mode.lower() != 'delete':
            raise RuntimeError('Database still has active readers; restore refused')
    database.close()


def restore_file(database, source):
    source = Path(source).resolve()
    manifest = json.loads(Path(str(source) + '.json').read_text(encoding='utf-8'))
    if manifest.get('format') != FORMAT or manifest.get('filename') != source.name or manifest.get('database_id') != database_id(database):
        raise ValueError('Backup manifest does not belong to this database')
    if manifest.get('sha256') != checksum(source):
        raise ValueError('Backup checksum mismatch')
    staging = database.path.with_name(database.path.name + '.restore-' + uuid.uuid4().hex)
    rollback = None
    with file_lock(str(database.path) + '.worker.lock', blocking=False), admission_lock(database):
        require_offline(database)
        exists = database.path.exists()
        if exists:
            database.check_schema()
            flush_deletions(database)
            with database.transaction() as db:
                identities = [(r.child_pid, r.child_created_at) for r in db.scalars(select(Run).where(Run.child_pid.is_not(None)))]
            for pid, created in identities:
                terminate_identity(pid, created)
        records = DeletionJournal(database.path).read()
        staged = None
        try:
            sqlite_copy(source, staging)
            staged = Database(staging)
            staged.migrate()
            with staged.transaction(immediate=True) as db:
                # Include unacknowledged outbox entries contained in the snapshot.
                deleted = {item['session_id']: item for item in records}
                for item in db.scalars(select(DeletedSession)):
                    deleted[item.session_id] = {'session_id': item.session_id, 'user_id': item.user_id, 'deleted_at': item.deleted_at}
                for item in deleted.values():
                    session = db.get(ChatSession, item['session_id'])
                    if session and session.user_id == item['user_id']:
                        db.delete(session)
                    if db.get(DeletedSession, item['session_id']) is None:
                        db.add(DeletedSession(session_id=item['session_id'], user_id=item['user_id'],
                                              deleted_at=item['deleted_at'], delivered=True))
                db.flush()
                for run in db.scalars(select(Run)):
                    if run.status in ACTIVE:
                        fail_run(db, run, status='interrupted', code='backup_restored')
                    run.child_pid = run.child_created_at = None
                db.execute(delete(AuthSession))  # Revoked logins must not resurrect from backups.
                db.execute(delete(ServiceInstance))
                state = db.get(WorkerState, 1)
                if state:
                    state.active = False
                if db.connection().exec_driver_sql('PRAGMA foreign_key_check').first():
                    raise RuntimeError('Restored database violates referential integrity')
            delete_mode(staged)
            if exists:
                backup_dir = database.path.parent / 'backups'
                rollback = backup_file(database, backup_dir / ('pre-restore-' + uuid.uuid4().hex + '.sqlite3'))['path']
                delete_mode(database)
            os.replace(staging, database.path)
            restored = Database(database.path)
            try:
                restored.migrate()  # Re-enable WAL and validate the installed schema.
            finally:
                restored.close()
            return {'restored': str(database.path), 'rollback_backup': rollback, 'deletion_records_replayed': len(deleted)}
        finally:
            if staged:
                staged.close()
            staging.unlink(missing_ok=True)
            # Staging sidecars belong solely to this unique temporary database.
            for suffix in ('-wal', '-shm'):
                Path(str(staging) + suffix).unlink(missing_ok=True)


def prune_backups(database, directory, days, *, now=None):
    now = time.time() if now is None else now
    directory = Path(directory).resolve()
    removed = 0
    for metadata in directory.glob('*.sqlite3.json'):
        try:
            manifest = json.loads(metadata.read_text(encoding='utf-8'))
            if not isinstance(manifest, dict):
                continue
            filename = manifest.get('filename', '')
            target = directory / filename
            if manifest.get('format') != FORMAT or manifest.get('database_id') != database_id(database):
                continue
            if Path(filename).name != filename or metadata.name != filename + '.json' or target.resolve().parent != directory:
                continue
            created = manifest.get('created_at')
            if not isinstance(created, (int, float)) or not math.isfinite(created):
                continue
            if created >= now - days * 86400 or not target.is_file() or checksum(target) != manifest.get('sha256'):
                continue
            target.unlink()
            metadata.unlink()
            removed += 1
        except (ValueError, OSError):
            continue  # An unrelated or modified file is never a pruning target.
    return removed
