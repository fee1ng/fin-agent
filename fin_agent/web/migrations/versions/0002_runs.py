"""Durable submissions, conversation snapshots, streams and per-call traces."""
from alembic import op
import sqlalchemy as sa

revision = '0002_runs'
down_revision = '0001_accounts'


def upgrade():
    op.create_table('runs',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.String(32), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('input', sa.Text, nullable=False),
        sa.Column('key', sa.String(128), nullable=False), sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('owner', sa.String(32)), sa.Column('received_at', sa.Float, nullable=False),
        sa.Column('started_at', sa.Float), sa.Column('finished_at', sa.Float),
        sa.Column('last_seq', sa.Integer, nullable=False), sa.Column('first_content_at', sa.Float),
        sa.UniqueConstraint('user_id', 'key'))
    op.create_index('uq_runs_active_session', 'runs', ['session_id'], unique=True,
                    sqlite_where=sa.text("status IN ('queued','running')"))
    op.create_index('ix_runs_status_received', 'runs', ['status', 'received_at'])
    op.create_table('messages',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('run_id', sa.String(32), sa.ForeignKey('runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(16), nullable=False), sa.Column('phase', sa.String(16), nullable=False),
        sa.Column('content', sa.Text, nullable=False), sa.Column('status', sa.String(16), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False))
    for column in ('session_id', 'run_id'):
        op.create_index('ix_messages_' + column, 'messages', [column])
    op.create_table('conversation_turns',
        sa.Column('session_id', sa.String(32), sa.ForeignKey('sessions.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('history', sa.JSON, nullable=False))
    op.create_table('run_events',
        sa.Column('run_id', sa.String(32), sa.ForeignKey('runs.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('seq', sa.Integer, primary_key=True), sa.Column('type', sa.String(40), nullable=False),
        sa.Column('data', sa.JSON, nullable=False), sa.Column('at', sa.Float, nullable=False))
    op.create_table('spans', sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('run_id', sa.String(32), sa.ForeignKey('runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False), sa.Column('started', sa.JSON, nullable=False),
        sa.Column('finished', sa.JSON), sa.Column('first_chunk', sa.JSON))
    op.create_index('ix_spans_run_id', 'spans', ['run_id'])


def downgrade():
    for table in ('spans', 'run_events', 'conversation_turns', 'messages', 'runs'):
        op.drop_table(table)
