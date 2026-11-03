"""Append-only application receipts; native Harness logs remain execution truth."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import uuid


class TraceStorageError(RuntimeError):
    pass


def validate_session_id(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("Invalid Harness session ID")


@contextmanager
def file_lock(path, *, blocking=True):
    """Cross-process lock; separate file handles serialize threads as well."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def fold_runs(records):
    """The same receipt snapshot must produce the same runtime and query state."""
    runs = {}
    for record in records:
        data = record['data']
        run_id = data.get('run_id')
        if record['type'] == 'received':
            runs[run_id] = {**data, 'timestamp': record['timestamp'],
                            'receipt_seq': record['seq'], 'records': []}
        if run_id in runs:
            runs[run_id]['records'].append(record)
            if record['type'] in {'started', 'finished'}:
                runs[run_id].update(data)
    return runs


class TraceStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal = self.directory / "receipts.jsonl"
        self.lock = self.directory / "receipts.lock"

    def session_lock(self, session_id):
        return file_lock(self.directory / "locks" / (hashlib.sha256(session_id.encode()).hexdigest() + ".lock"))

    def _read(self):
        if not self.journal.exists():
            return []
        records = []
        with self.journal.open("rb") as source:
            for line in source:
                if not line.endswith(b"\n"):
                    raise TraceStorageError("storage_error: incomplete receipt journal tail")
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise TraceStorageError("storage_error: invalid receipt journal") from exc
                if record.get("seq") != len(records):
                    raise TraceStorageError("storage_error: receipt sequence gap")
                records.append(record)
        return records

    def records(self):
        with file_lock(self.lock):
            return self._read()

    def repair_tail(self):
        """Preserve a torn write for diagnosis, retaining every complete receipt."""
        with file_lock(self.lock):
            if not self.journal.exists():
                return None
            raw = self.journal.read_bytes()
            if not raw or raw.endswith(b'\n'):
                self._read()  # Never repair corruption inside complete records.
                return None
            boundary = raw.rfind(b'\n') + 1
            prefix, tail = raw[:boundary], raw[boundary:]
            try:
                records = [json.loads(line) for line in prefix.splitlines()]
                if any(record.get('seq') != seq for seq, record in enumerate(records)):
                    raise ValueError('sequence gap')
            except (ValueError, UnicodeError) as exc:
                raise TraceStorageError('storage_error: corruption precedes torn journal tail') from exc
            archive = self.directory / ('torn-receipt-' + uuid.uuid4().hex + '.bin')
            with archive.open('xb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            sync_directory(self.directory)
            with self.journal.open('r+b') as target:
                target.truncate(boundary)
                target.flush()
                os.fsync(target.fileno())
            self._append(records, 'journal_tail_recovered', {
                'archive': archive.name, 'discarded_uncommitted_bytes': len(tail),
                'source_sha256': hashlib.sha256(raw).hexdigest()})
            return archive.name

    def _append(self, records, kind, data):
        record = {"version": 1, "seq": len(records), "type": kind,
                  "timestamp": datetime.now(timezone.utc).isoformat(), "data": data}
        encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
        try:
            with self.journal.open("ab") as target:
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            sync_directory(self.directory)
        except OSError as exc:
            raise TraceStorageError("storage_error: receipt persistence failed") from exc
        return record

    def append(self, kind, **data):
        with file_lock(self.lock):
            return self._append(self._read(), kind, data)

    @contextmanager
    def owned_accept(self, text, *, session_id, input_id, metadata):
        lease = None
        try:
            with file_lock(self.lock):
                records = self._read()
                receipt, accepted = self._accept(records, text, session_id, input_id, metadata)
                if accepted:
                    lease = file_lock(self.lease_path(receipt['run_id']))
                    lease.__enter__()
                    self._append(records, 'received', receipt)
            yield receipt, accepted
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)

    def lease_path(self, run_id):
        return self.directory / 'leases' / (hashlib.sha256(run_id.encode()).hexdigest() + '.lock')

    def _accept(self, records, text, session_id, input_id, metadata):
        for record in records:
            data = record['data']
            if record['type'] == 'received' and data['input_id'] == input_id:
                if data['application_session_id'] != session_id or data['input'] != text:
                    raise ValueError('input_id already belongs to different request content')
                return data, False
        return {'run_id': 'run-' + uuid.uuid4().hex, 'input_id': input_id,
                'application_session_id': session_id, 'harness_session_id': session_id,
                'input': text, 'status': 'queued', 'metadata': metadata}, True

    def accept(self, text, *, session_id, input_id, metadata):
        with file_lock(self.lock):
            records = self._read()
            receipt, accepted = self._accept(records, text, session_id, input_id, metadata)
            if accepted:
                self._append(records, 'received', receipt)
            return receipt, accepted

    def get_run(self, run_id):
        return fold_runs(self.records())[run_id]

    def native_events(self, session_id):
        # IDs come from persisted receipts, never interpolate arbitrary paths.
        validate_session_id(session_id)
        root = self.directory / "runtime" / session_id / "native"
        paths = list(root.glob(f"sessions/*/{session_id}/session.v4.jsonl"))
        if not paths:
            return []
        if len(paths) != 1:
            raise TraceStorageError("Ambiguous native session location")
        raw = paths[0].read_bytes()
        if raw and not raw.endswith(b'\n'):
            raise TraceStorageError('storage_error: incomplete native session log tail')
        try:
            return [json.loads(line) for line in raw.splitlines()]
        except (ValueError, UnicodeError) as exc:
            raise TraceStorageError('storage_error: invalid native session log') from exc
