import time
import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, JSON, text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def identifier():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default='user')
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    expires_at: Mapped[float] = mapped_column(Float)


class LoginFailure(Base):
    __tablename__ = 'login_failures'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(64))
    at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (Index('ix_login_failure_address_at', 'address', 'at'),)


class ChatSession(Base):
    __tablename__ = 'sessions'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    title: Mapped[str] = mapped_column(String(120), default='新会话')
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (Index('ix_sessions_owner_updated', 'user_id', 'updated_at'),)


class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id', ondelete='CASCADE'))
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    status: Mapped[str] = mapped_column(String(16), default='queued')
    input: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    first_content_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    retry_of: Mapped[str | None] = mapped_column(String(32), nullable=True)
    input_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    child_created_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    trace_purged_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    events_purged_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    trace_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (
        UniqueConstraint('user_id', 'key'),
        Index('uq_runs_active_session', 'session_id', unique=True,
              sqlite_where=text("status IN ('queued','running')")),
        Index('ix_runs_status_received', 'status', 'received_at'),
        Index('ix_runs_user_received', 'user_id', 'received_at'),
        Index('ix_runs_session_received', 'session_id', 'received_at'),
    )


class Message(Base):
    __tablename__ = 'messages'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id', ondelete='CASCADE'), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id', ondelete='CASCADE'), index=True)
    role: Mapped[str] = mapped_column(String(16))
    phase: Mapped[str] = mapped_column(String(16), default='final')
    content: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(16), default='draft')
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Conversation(Base):
    __tablename__ = 'conversation_turns'
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id', ondelete='CASCADE'), primary_key=True)
    history: Mapped[list] = mapped_column(JSON)


class RunEvent(Base):
    __tablename__ = 'run_events'
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id', ondelete='CASCADE'), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON)
    at: Mapped[float] = mapped_column(Float, default=time.time)


class Span(Base):
    __tablename__ = 'spans'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id', ondelete='CASCADE'), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    started: Mapped[dict] = mapped_column(JSON)
    finished: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_chunk: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (Index('ix_spans_run_kind', 'run_id', 'kind'),)


class WorkerState(Base):
    __tablename__ = 'worker_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epoch: Mapped[str] = mapped_column(String(32))
    pid: Mapped[int] = mapped_column(Integer)
    heartbeat_at: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean)


class DeletedSession(Base):
    __tablename__ = 'deleted_sessions'
    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32))
    deleted_at: Mapped[float] = mapped_column(Float, default=time.time)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class ServiceInstance(Base):
    __tablename__ = 'service_instances'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pid: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)
