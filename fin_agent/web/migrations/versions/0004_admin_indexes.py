"""Indexes for administrator filters and per-run call lookup."""
from alembic import op

revision = '0004_admin_indexes'
down_revision = '0003_recovery'


def upgrade():
    op.create_index('ix_runs_user_received', 'runs', ['user_id', 'received_at'])
    op.create_index('ix_runs_session_received', 'runs', ['session_id', 'received_at'])
    op.create_index('ix_spans_run_kind', 'spans', ['run_id', 'kind'])


def downgrade():
    op.drop_index('ix_spans_run_kind', 'spans')
    op.drop_index('ix_runs_session_received', 'runs')
    op.drop_index('ix_runs_user_received', 'runs')
