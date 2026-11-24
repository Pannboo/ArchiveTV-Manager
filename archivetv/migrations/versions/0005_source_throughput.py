"""source bitrate and measured throughput

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08 12:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('playback_sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bitrate_kbps', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('throughput_kbps', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('playback_sources', schema=None) as batch_op:
        batch_op.drop_column('throughput_kbps')
        batch_op.drop_column('bitrate_kbps')
