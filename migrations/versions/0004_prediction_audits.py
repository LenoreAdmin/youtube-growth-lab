"""V3 reproducible prediction and decision audit; additive only."""
from alembic import op
import sqlalchemy as sa
revision = "0004"
down_revision = "0003"


def upgrade():
    op.create_table("prediction_audits",
        sa.Column("forecast_id", sa.Integer(), sa.ForeignKey("forecasts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model_run_id", sa.Integer(), sa.ForeignKey("model_runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("feedback", sa.JSON(), nullable=False),
        sa.Column("recommendations", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("prediction_audits")
