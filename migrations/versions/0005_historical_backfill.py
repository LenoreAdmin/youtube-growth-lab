"""Historical backfill tables; additive only, existing production data untouched."""
from alembic import op
import sqlalchemy as sa
revision = "0005"
down_revision = "0004"


def upgrade():
    op.create_table("backfill_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False))
    op.create_table("backfill_progress",
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("first", sa.Date(), nullable=False),
        sa.Column("target", sa.Date(), nullable=False),
        sa.Column("through", sa.Date(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "kind"))
    op.create_table("video_traffic_daily",
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=False),
        sa.Column("watch_minutes", sa.Float(), nullable=False),
        sa.Column("paid", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "day", "source"))
    op.create_table("backfill_reports",
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("start", sa.Date(), nullable=False),
        sa.Column("end", sa.Date(), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("video_id", "kind", "start", "end"))


def downgrade():
    op.drop_table("backfill_reports")
    op.drop_table("video_traffic_daily")
    op.drop_table("backfill_progress")
    op.drop_table("backfill_runs")
