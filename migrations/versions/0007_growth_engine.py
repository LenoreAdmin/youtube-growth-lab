"""V5 growth engine: scores, actions and daily plans; additive only."""
from alembic import op
import sqlalchemy as sa
revision = "0007"
down_revision = "0006"


def upgrade():
    op.create_table("growth_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("opportunity", sa.JSON(), nullable=False),
        sa.Column("viewer", sa.JSON(), nullable=False),
        sa.Column("subscriber", sa.JSON(), nullable=False),
        sa.Column("revival", sa.JSON(), nullable=False),
        sa.Column("momentum", sa.JSON(), nullable=False),
        sa.UniqueConstraint("video_id", "day", "version"))
    op.create_index("ix_growth_scores_video_id", "growth_scores", ["video_id"])
    op.create_table("growth_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_metric", sa.String(32), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("evaluate_after", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("evaluation", sa.JSON(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("video_id", "created_day"))
    op.create_index("ix_growth_actions_video_id", "growth_actions", ["video_id"])
    op.create_table("growth_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.UniqueConstraint("day", "version"))
    op.create_index("ix_growth_plans_day", "growth_plans", ["day"])


def downgrade():
    op.drop_table("growth_plans")
    op.drop_table("growth_actions")
    op.drop_table("growth_scores")
