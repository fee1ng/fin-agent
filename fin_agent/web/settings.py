import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database: Path
    origin: str = 'https://fin-agent.internal'
    secure_cookie: bool = True
    session_seconds: int = 43200
    concurrency: int = 2
    queue_limit: int = 16
    user_run_limit: int = 2
    run_timeout: float = 300
    trace_days: int = 30
    event_hours: int = 24
    backup_days: int = 7

    def __post_init__(self):
        origin = urlsplit(self.origin)
        if origin.scheme not in {'http', 'https'} or not origin.netloc or origin.path or origin.query or origin.fragment:
            raise ValueError('origin must be a complete origin without a path')
        if self.secure_cookie and origin.scheme != 'https':
            raise ValueError('Secure cookies require an HTTPS origin')
        if not self.secure_cookie and origin.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('Insecure development cookies are restricted to loopback')
        if self.session_seconds <= 0:
            raise ValueError('session_seconds must be positive')
        if min(self.concurrency, self.queue_limit, self.user_run_limit, self.run_timeout) <= 0:
            raise ValueError('Execution limits must be positive')
        if min(self.trace_days, self.event_hours, self.backup_days) < 1:
            raise ValueError('Retention periods must be at least one day/hour')

    @classmethod
    def from_env(cls):
        return cls(Path(os.getenv('FIN_WEB_DATABASE', '.local/web.sqlite3')),
                   os.getenv('FIN_WEB_ORIGIN', 'https://fin-agent.internal'),
                   os.getenv('FIN_WEB_DEV_HTTP', '0') != '1',
                   concurrency=int(os.getenv('FIN_WEB_CONCURRENCY', '2')),
                   queue_limit=int(os.getenv('FIN_WEB_QUEUE_LIMIT', '16')),
                   run_timeout=float(os.getenv('FIN_WEB_RUN_TIMEOUT', '300')),
                   trace_days=int(os.getenv('FIN_WEB_TRACE_DAYS', '30')),
                   event_hours=int(os.getenv('FIN_WEB_EVENT_HOURS', '24')),
                   backup_days=int(os.getenv('FIN_WEB_BACKUP_DAYS', '7')))
