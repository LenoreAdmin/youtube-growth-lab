"""Inventory of the channel's own playlists.

Without it the system cannot know whether a playlist exists, and an experiment must never
presuppose a resource it has not verified.
"""
from alembic import op
import sqlalchemy as sa
revision = "0011"
down_revision = "0010"


def upgrade():
    op.create_table("channel_playlists",
                    sa.Column("id", sa.String(64), primary_key=True),
                    sa.Column("title", sa.String(256), nullable=False),
                    sa.Column("item_count", sa.Integer(), nullable=True),
                    sa.Column("privacy", sa.String(32), nullable=True),
                    sa.Column("first_seen_day", sa.Date(), nullable=False),
                    sa.Column("last_seen_day", sa.Date(), nullable=False),
                    sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_channel_playlists_last_seen_day", "channel_playlists", ["last_seen_day"])


def downgrade():
    op.drop_index("ix_channel_playlists_last_seen_day", table_name="channel_playlists")
    op.drop_table("channel_playlists")
