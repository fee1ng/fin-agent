"""Durable execution identity and explicit retries without duplicate questions."""
from alembic import op
import sqlalchemy as sa

revision = '0003_recovery'
down_revision = '0002_runs'


def upgrade():
    for name, kind in [('retry_of', sa.String(32)), ('input_message_id', sa.String(64)),
                       ('child_pid', sa.Integer()), ('child_created_at', sa.Float())]:
        op.add_column('runs', sa.Column(name, kind, nullable=True))
    op.execute("UPDATE runs SET input_message_id=(SELECT id FROM messages WHERE messages.run_id=runs.id AND role='user' LIMIT 1)")
    op.create_table('worker_state', sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('epoch', sa.String(32), nullable=False), sa.Column('pid', sa.Integer, nullable=False),
        sa.Column('heartbeat_at', sa.Float, nullable=False), sa.Column('active', sa.Boolean, nullable=False))


def downgrade():
    op.drop_table('worker_state')
    for name in ('child_created_at', 'child_pid', 'input_message_id', 'retry_of'):
        op.drop_column('runs', name)
