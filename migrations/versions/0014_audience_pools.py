"""Public playlists and channels found by probing YouTube: audiences that do not know us yet."""
from alembic import op
import sqlalchemy as sa
revision = "0014"
down_revision = "0013"


def upgrade():
    op.create_table("audience_pools",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("kind", sa.String(32), nullable=False),
                    sa.Column("key", sa.String(64), nullable=False),
                    sa.Column("title", sa.String(300), nullable=False),
                    sa.Column("url", sa.String(500), nullable=False),
                    sa.Column("channel_id", sa.String(64), nullable=True),
                    sa.Column("channel_title", sa.String(256), nullable=True),
                    sa.Column("item_count", sa.Integer(), nullable=True),
                    sa.Column("subscribers", sa.BigInteger(), nullable=True),
                    sa.Column("views", sa.BigInteger(), nullable=True),
                    sa.Column("description", sa.String(1000), nullable=True),
                    sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
                    sa.Column("details", sa.JSON(), nullable=False),
                    sa.Column("query", sa.String(200), nullable=True),
                    sa.Column("first_seen_day", sa.Date(), nullable=False),
                    sa.Column("last_seen_day", sa.Date(), nullable=False),
                    sa.UniqueConstraint("kind", "key", name="uq_audience_pools_kind_key"))
    op.create_index("ix_audience_pools_last_seen_day", "audience_pools", ["last_seen_day"])


def downgrade():
    op.drop_index("ix_audience_pools_last_seen_day", table_name="audience_pools")
    op.drop_table("audience_pools")
