"""Content Intelligence: eigene Mediendateien lokal verstehen, nur Messwerte nach Neon.

Additiv. Die Mediendateien selbst bleiben lokal; hier liegen ausschliesslich abgeleitete Messwerte,
Zeitmarken und die Verbindung vom Kandidaten zum spaeter veroeffentlichten eigenen Asset.
"""
from alembic import op
import sqlalchemy as sa
revision = "0018"
down_revision = "0017"


def upgrade():
    op.create_table("media_assets",
                    sa.Column("id", sa.String(64), primary_key=True),
                    sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="SET NULL")),
                    sa.Column("path", sa.String(1024), nullable=False),
                    sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
                    sa.Column("duration_seconds", sa.Float()),
                    sa.Column("has_video", sa.Boolean(), nullable=False, server_default=sa.false()),
                    sa.Column("has_audio", sa.Boolean(), nullable=False, server_default=sa.false()),
                    sa.Column("status", sa.String(32), nullable=False, server_default="analysed"),
                    sa.Column("note", sa.String(1000)),
                    sa.Column("tools", sa.JSON(), nullable=False, server_default="{}"),
                    sa.Column("analysed_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_media_assets_video_id", "media_assets", ["video_id"])
    op.create_table("content_sections",
                    sa.Column("asset_id", sa.String(64), sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
                              primary_key=True),
                    sa.Column("idx", sa.Integer(), primary_key=True),
                    sa.Column("start_seconds", sa.Float(), nullable=False),
                    sa.Column("end_seconds", sa.Float(), nullable=False),
                    sa.Column("energy", sa.Float()),
                    sa.Column("energy_rel", sa.Float()),
                    sa.Column("repetition_strength", sa.Float()),
                    sa.Column("repetition_group", sa.Integer()),
                    sa.Column("vocal_presence", sa.Float()),
                    sa.Column("novelty", sa.Float()),
                    sa.Column("visual_cuts", sa.Integer()),
                    sa.Column("visual_cut_density", sa.Float()),
                    sa.Column("brightness", sa.Float()),
                    sa.Column("boundary_cut_distance", sa.Float()))
    op.create_table("content_lines",
                    sa.Column("asset_id", sa.String(64), sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
                              primary_key=True),
                    sa.Column("idx", sa.Integer(), primary_key=True),
                    sa.Column("start_seconds", sa.Float(), nullable=False),
                    sa.Column("end_seconds", sa.Float(), nullable=False),
                    sa.Column("text", sa.String(1000), nullable=False),
                    sa.Column("confidence", sa.Float()),
                    sa.Column("source", sa.String(16), nullable=False, server_default="asr"))
    op.create_table("content_candidates",
                    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
                    sa.Column("asset_id", sa.String(64), sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
                              nullable=False),
                    sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE")),
                    sa.Column("start_seconds", sa.Float(), nullable=False),
                    sa.Column("end_seconds", sa.Float(), nullable=False),
                    sa.Column("properties", sa.JSON(), nullable=False, server_default="{}"),
                    sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"),
                    sa.Column("hook", sa.String(1000)),
                    sa.Column("hook_source", sa.String(16)),
                    sa.Column("render_path", sa.String(1024)),
                    sa.Column("status", sa.String(32), nullable=False, server_default="open"),
                    sa.Column("created_day", sa.Date(), nullable=False),
                    sa.Column("published_video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="SET NULL")),
                    sa.Column("published_day", sa.Date()),
                    sa.UniqueConstraint("asset_id", "start_seconds", "end_seconds", name="uq_candidate_window"))
    op.create_index("ix_content_candidates_asset_id", "content_candidates", ["asset_id"])
    op.create_index("ix_content_candidates_video_id", "content_candidates", ["video_id"])


def downgrade():
    op.drop_table("content_candidates")
    op.drop_table("content_lines")
    op.drop_table("content_sections")
    op.drop_index("ix_media_assets_video_id", table_name="media_assets")
    op.drop_table("media_assets")
