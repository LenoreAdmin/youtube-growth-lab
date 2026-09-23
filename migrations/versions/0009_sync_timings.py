"""Per-step sync timings so a saturated budget can be diagnosed instead of guessed; additive."""
from alembic import op
import sqlalchemy as sa
revision = "0009"
down_revision = "0008"


def upgrade():
    op.add_column("sync_runs", sa.Column("timings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column("sync_runs", "timings")
