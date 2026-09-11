"""V4 historical learning, backtests, analytics forecasts and strategy log; additive only."""
from alembic import op
import sqlalchemy as sa
revision = "0006"
down_revision = "0005"


def upgrade():
    op.create_table("learning_datasets",
        sa.Column("signature", sa.String(64), primary_key=True),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("audit", sa.JSON(), nullable=False),
        sa.Column("rows_per_horizon", sa.JSON(), nullable=False),
        sa.Column("exclusions", sa.JSON(), nullable=False),
        sa.Column("baselines", sa.JSON(), nullable=False))
    op.create_table("learning_backtests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_signature", sa.String(64), sa.ForeignKey("learning_datasets.signature", ondelete="CASCADE"), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("champion", sa.String(64), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.UniqueConstraint("dataset_signature", "horizon_hours", "version"))
    op.create_index("ix_learning_backtests_dataset_signature", "learning_backtests", ["dataset_signature"])
    op.create_table("analytics_forecasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("origin_day", sa.Date(), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("dataset_signature", sa.String(64), nullable=True),
        sa.Column("predicted_views", sa.Float(), nullable=False),
        sa.Column("baseline_views", sa.Float(), nullable=False),
        sa.Column("lower_views", sa.Float(), nullable=True),
        sa.Column("upper_views", sa.Float(), nullable=True),
        sa.Column("interval_kind", sa.String(32), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("regime", sa.String(32), nullable=False),
        sa.Column("actual_views", sa.BigInteger(), nullable=True),
        sa.Column("absolute_error", sa.Float(), nullable=True),
        sa.Column("log_error", sa.Float(), nullable=True),
        sa.Column("eligibility", sa.String(64), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("video_id", "origin_day", "horizon_hours"))
    op.create_index("ix_analytics_forecasts_video_id", "analytics_forecasts", ["video_id"])
    op.create_index("ix_analytics_forecasts_origin_day", "analytics_forecasts", ["origin_day"])
    op.create_table("strategy_recommendations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("origin_day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("regime", sa.String(32), nullable=False),
        sa.Column("recommendation", sa.JSON(), nullable=False),
        sa.Column("forecast_ids", sa.JSON(), nullable=False),
        sa.Column("decision_ids", sa.JSON(), nullable=False),
        sa.UniqueConstraint("video_id", "origin_day", "version"))
    op.create_index("ix_strategy_recommendations_video_id", "strategy_recommendations", ["video_id"])


def downgrade():
    op.drop_table("strategy_recommendations")
    op.drop_table("analytics_forecasts")
    op.drop_table("learning_backtests")
    op.drop_table("learning_datasets")
