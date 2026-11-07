"""Versioned Web identity and isolated chat sessions."""
from alembic import op
import sqlalchemy as sa

revision = '0001_accounts'
down_revision = None


def upgrade():
    op.create_table('users',
                    sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('username', sa.String(64), nullable=False, unique=True),
                    sa.Column('password_hash', sa.Text, nullable=False),
                    sa.Column('role', sa.String(16), nullable=False),
                    sa.Column('disabled', sa.Boolean, nullable=False))
    op.create_table('auth_sessions',
                    sa.Column('token_hash', sa.String(64), primary_key=True),
                    sa.Column('user_id', sa.String(32), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
                    sa.Column('expires_at', sa.Float, nullable=False))
    op.create_index('ix_auth_sessions_user_id', 'auth_sessions', ['user_id'])
    op.create_table('login_failures',
                    sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
                    sa.Column('address', sa.String(64), nullable=False),
                    sa.Column('username', sa.String(64), nullable=False),
                    sa.Column('at', sa.Float, nullable=False))
    op.create_index('ix_login_failure_address_at', 'login_failures', ['address', 'at'])
    op.create_table('sessions',
                    sa.Column('id', sa.String(32), primary_key=True),
                    sa.Column('user_id', sa.String(32), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
                    sa.Column('title', sa.String(120), nullable=False),
                    sa.Column('created_at', sa.Float, nullable=False),
                    sa.Column('updated_at', sa.Float, nullable=False))
    op.create_index('ix_sessions_owner_updated', 'sessions', ['user_id', 'updated_at'])


def downgrade():
    for table in ('sessions', 'login_failures', 'auth_sessions', 'users'):
        op.drop_table(table)
