"""Deletion outbox delivery: acknowledged deletes survive restoring older DBs."""
import json
import os
from pathlib import Path

from sqlalchemy import select

from fin_agent.agent.trace_store import file_lock
from fin_agent.web.models import DeletedSession


class DeletionJournal:
    def __init__(self, database_path):
        self.path = Path(str(database_path) + '.deletions.jsonl')
        self.lock = Path(str(self.path) + '.lock')

    def _read(self):
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_bytes().splitlines(keepends=True):
            if not line.endswith(b'\n'):
                raise RuntimeError('Deletion journal has an incomplete record')
            item = json.loads(line)
            if item.get('seq') != len(result) or not isinstance(item.get('session_id'), str) or not isinstance(item.get('user_id'), str):
                raise RuntimeError('Deletion journal is invalid')
            result.append(item)
        return result

    def read(self):
        with file_lock(self.lock):
            return self._read()

    def deliver(self, records):
        with file_lock(self.lock):
            previous = self._read()
            known = {item['session_id'] for item in previous}
            with self.path.open('ab') as target:
                for item in records:
                    if item['session_id'] in known:
                        continue
                    payload = {**item, 'seq': len(previous)}
                    target.write((json.dumps(payload) + '\n').encode())
                    previous.append(payload)
                    known.add(item['session_id'])
                target.flush()
                os.fsync(target.fileno())


def flush_deletions(database):
    with database.transaction() as db:
        rows = db.scalars(select(DeletedSession).where(DeletedSession.delivered.is_(False))).all()
        records = [{'session_id': r.session_id, 'user_id': r.user_id, 'deleted_at': r.deleted_at} for r in rows]
    if not records:
        return
    DeletionJournal(database.path).deliver(records)
    with database.transaction(immediate=True) as db:
        for item in records:
            row = db.get(DeletedSession, item['session_id'])
            if row:
                row.delivered = True
