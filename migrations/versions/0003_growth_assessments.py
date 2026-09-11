"""Add observation-time growth log without changing existing production tables."""
from alembic import op
import sqlalchemy as sa
revision = "0003"
down_revision = "0002"


def upgrade():
    op.create_table("growth_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("origin_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("assessment", sa.JSON(), nullable=False),
        sa.UniqueConstraint("video_id", "origin_at", "version"))
    op.create_index("ix_growth_assessments_video_id", "growth_assessments", ["video_id"])
    op.create_index("ix_growth_assessments_origin_at", "growth_assessments", ["origin_at"])

    op.create_table("experiment_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("decision_id", sa.Integer(), sa.ForeignKey("decisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dimension", sa.String(32), nullable=False),
        sa.Column("before_value", sa.String(2000), nullable=False),
        sa.Column("after_value", sa.String(2000), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False))
    op.create_index("ix_experiment_changes_decision_id", "experiment_changes", ["decision_id"])


def downgrade():
    op.drop_table("experiment_changes")
    op.drop_table("growth_assessments")
