"""Independent trace lifetimes and durable deletion delivery."""
from alembic import op
import sqlalchemy as sa

revision = '0005_retention'
down_revision = '0004_admin_indexes'


def upgrade():
    for name, kind in [('trace_purged_at', sa.Float()), ('events_purged_at', sa.Float()), ('trace_summary', sa.JSON())]:
        op.add_column('runs', sa.Column(name, kind, nullable=True))
    op.create_table('deleted_sessions', sa.Column('session_id', sa.String(32), primary_key=True),
        sa.Column('user_id', sa.String(32), nullable=False), sa.Column('deleted_at', sa.Float, nullable=False),
        sa.Column('delivered', sa.Boolean, nullable=False))
    op.create_table('service_instances', sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('pid', sa.Integer, nullable=False), sa.Column('created_at', sa.Float, nullable=False))


def downgrade():
    op.drop_table('service_instances')
    op.drop_table('deleted_sessions')
    for name in ('trace_summary', 'events_purged_at', 'trace_purged_at'):
        op.drop_column('runs', name)
