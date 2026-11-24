"""channel packs, generated breaks, channel looks and logos

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'channel_packs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key', sa.String(64), nullable=False, unique=True),
        sa.Column('name', sa.String(256), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('channel_id', sa.Integer(), sa.ForeignKey('channels.id', ondelete='SET NULL'), nullable=True),
        sa.Column('policy', sa.String(16), nullable=False, server_default='inbox'),
        sa.Column('target_hours', sa.Float(), nullable=False, server_default='100'),
        sa.Column('theme_check', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('spec', sa.JSON(), nullable=False),
        sa.Column('installed_at', sa.DateTime(), nullable=False),
        sa.Column('last_fill_at', sa.DateTime(), nullable=True),
        sa.Column('last_fill_status', sa.String(32), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('aired_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('inbox_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('refresh_days', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('rotate_percent', sa.Integer(), nullable=False, server_default='15'),
        sa.Column('last_refresh_at', sa.DateTime(), nullable=True),
        sa.Column('last_refresh', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('state', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index('ix_channel_packs_channel_id', 'channel_packs', ['channel_id'])
    op.create_table(
        'followed_channels',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('pack_id', sa.Integer(), sa.ForeignKey('channel_packs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('channel_id', sa.String(64), nullable=False),
        sa.Column('title', sa.String(256), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('hits', sa.Integer(), nullable=False),
        sa.Column('samples', sa.JSON(), nullable=False),
        sa.Column('ai_verdict', sa.JSON(), nullable=False),
        sa.Column('rule_id', sa.Integer(), sa.ForeignKey('discovery_rules.id', ondelete='SET NULL'), nullable=True),
        sa.Column('added_by', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('followed_at', sa.DateTime(), nullable=True),
        sa.Column('last_rss_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('pack_id', 'channel_id'),
    )
    op.create_index('ix_followed_channels_pack_id', 'followed_channels', ['pack_id'])
    with op.batch_alter_table('media_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ai_meta', sa.JSON(), nullable=False, server_default='{}'))
    with op.batch_alter_table('discovery_rules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pack_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_discovery_rules_pack_id', ['pack_id'])
        batch_op.create_foreign_key('fk_discovery_rules_pack_id', 'channel_packs', ['pack_id'], ['id'], ondelete='CASCADE')
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.add_column(sa.Column('breaks_enabled', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('break_seconds', sa.Integer(), nullable=False, server_default='12'))
        batch_op.add_column(sa.Column('card_theme', sa.String(32), nullable=False, server_default='auto'))
        batch_op.add_column(sa.Column('break_style', sa.String(16), nullable=False, server_default='auto'))
        batch_op.add_column(sa.Column('logo_generated', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('media_items', schema=None) as batch_op:
        batch_op.drop_column('ai_meta')
    op.drop_index('ix_followed_channels_pack_id', 'followed_channels')
    op.drop_table('followed_channels')
    with op.batch_alter_table('channels', schema=None) as batch_op:
        batch_op.drop_column('logo_generated')
        batch_op.drop_column('break_style')
        batch_op.drop_column('card_theme')
        batch_op.drop_column('break_seconds')
        batch_op.drop_column('breaks_enabled')
    with op.batch_alter_table('discovery_rules', schema=None) as batch_op:
        batch_op.drop_constraint('fk_discovery_rules_pack_id', type_='foreignkey')
        batch_op.drop_index('ix_discovery_rules_pack_id')
        batch_op.drop_column('pack_id')
    op.drop_index('ix_channel_packs_channel_id', 'channel_packs')
    op.drop_table('channel_packs')
