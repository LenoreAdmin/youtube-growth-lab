"""V6 external audience discovery: runs, quota, query cache, public items/channels, own signals, opportunity memory."""
from alembic import op
import sqlalchemy as sa
revision = "0008"
down_revision = "0007"


def upgrade():
    op.create_table("discovery_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("units_used", sa.Integer(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False))
    op.create_index("ix_discovery_runs_day", "discovery_runs", ["day"])
    op.create_table("discovery_quota",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("discovery_queries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("query", sa.String(200), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("seed_video_id", sa.String(64), nullable=True),
        sa.Column("priority", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_probed_day", sa.Date(), nullable=True),
        sa.Column("next_probe_day", sa.Date(), nullable=True),
        sa.Column("probe_count", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.UniqueConstraint("query"))
    op.create_table("discovery_items",
        sa.Column("video_id", sa.String(64), primary_key=True),
        sa.Column("channel_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("channel_title", sa.String(256), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("views", sa.BigInteger(), nullable=True),
        sa.Column("likes", sa.BigInteger(), nullable=True),
        sa.Column("comments", sa.BigInteger(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("via", sa.JSON(), nullable=False),
        sa.Column("first_seen_day", sa.Date(), nullable=False),
        sa.Column("last_seen_day", sa.Date(), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False))
    op.create_index("ix_discovery_items_channel_id", "discovery_items", ["channel_id"])
    op.create_table("discovery_channels",
        sa.Column("channel_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("subscribers", sa.BigInteger(), nullable=True),
        sa.Column("video_count", sa.Integer(), nullable=True),
        sa.Column("views", sa.BigInteger(), nullable=True),
        sa.Column("first_seen_day", sa.Date(), nullable=False),
        sa.Column("last_seen_day", sa.Date(), nullable=False))
    op.create_table("discovery_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(512), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=False),
        sa.Column("watch_minutes", sa.Float(), nullable=False),
        sa.Column("fetched_day", sa.Date(), nullable=False),
        sa.UniqueConstraint("video_id", "kind", "detail", "window_end"))
    op.create_index("ix_discovery_signals_video_id", "discovery_signals", ["video_id"])
    op.create_table("discovery_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("gap", sa.String(40), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("evaluation", sa.JSON(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("day", "kind", "key"))
    op.create_index("ix_discovery_opportunities_day", "discovery_opportunities", ["day"])
    op.create_index("ix_discovery_opportunities_video_id", "discovery_opportunities", ["video_id"])


def downgrade():
    op.drop_table("discovery_opportunities")
    op.drop_table("discovery_signals")
    op.drop_table("discovery_channels")
    op.drop_table("discovery_items")
    op.drop_table("discovery_queries")
    op.drop_table("discovery_quota")
    op.drop_table("discovery_runs")
