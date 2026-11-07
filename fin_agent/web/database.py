from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker


class Database:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(URL.create('sqlite', database=str(self.path)),
                                    connect_args={'check_same_thread': False, 'timeout': 10})

        @event.listens_for(self.engine, 'connect')
        def configure(connection, record):
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=10000')

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self, *, immediate=False):
        with self.sessions.begin() as db:
            if immediate:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            yield db

    def migration_config(self):
        cfg = Config()
        cfg.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
        return cfg

    def migrate(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA journal_mode=WAL')
            connection.commit()
            cfg = self.migration_config()
            cfg.attributes['connection'] = connection
            command.upgrade(cfg, 'head')

    def check_schema(self):
        head = ScriptDirectory.from_config(self.migration_config()).get_current_head()
        with self.engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        if current != head:
            raise RuntimeError('Web database needs migration: python -m fin_agent.web migrate')

    def close(self):
        self.engine.dispose()
