"""Retentionskurve je Video: Grundlage fuer abgeleitete Shorts-/Rework-Empfehlungen."""
from alembic import op
import sqlalchemy as sa
revision = "0017"
down_revision = "0016"


def upgrade():
    op.create_table("video_retention",
                    sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"),
                              primary_key=True),
                    sa.Column("rows", sa.JSON(), nullable=False, server_default="[]"),
                    sa.Column("window_start", sa.Date(), nullable=False),
                    sa.Column("window_end", sa.Date(), nullable=False),
                    sa.Column("fetched_day", sa.Date(), nullable=False))


def downgrade():
    op.drop_table("video_retention")
