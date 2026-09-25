"""Eigene oeffentliche Metadaten je Video: Beschreibung, Tags, Themenkategorien, Kanalangaben."""
from alembic import op
import sqlalchemy as sa
revision = "0016"
down_revision = "0015"


def upgrade():
    op.create_table("video_profiles",
                    sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"),
                              primary_key=True),
                    sa.Column("description", sa.String(5000)),
                    sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
                    sa.Column("topics", sa.JSON(), nullable=False, server_default="[]"),
                    sa.Column("category_id", sa.String(16)),
                    sa.Column("channel_title", sa.String(256)),
                    sa.Column("channel_description", sa.String(2000)),
                    sa.Column("channel_keywords", sa.String(1000)),
                    sa.Column("channel_topics", sa.JSON(), nullable=False, server_default="[]"),
                    sa.Column("fetched_day", sa.Date(), nullable=False))


def downgrade():
    op.drop_table("video_profiles")
