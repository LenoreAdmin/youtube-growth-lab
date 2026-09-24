"""Real action lifecycle: a proposal is not a running experiment.

Adds the confirmation fields and reclassifies every stored "pending" row as "proposed":
no execution was ever recorded for them, and the system cannot execute anything itself.
"""
from alembic import op
import sqlalchemy as sa
revision = "0010"
down_revision = "0009"


def upgrade():
    op.add_column("growth_actions", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("growth_actions", sa.Column("started_day", sa.Date(), nullable=True))
    op.add_column("growth_actions", sa.Column("baseline", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.execute("UPDATE growth_actions SET status = 'proposed' WHERE status = 'pending'")


def downgrade():
    op.execute("UPDATE growth_actions SET status = 'pending' WHERE status IN ('proposed', 'running')")
    op.drop_column("growth_actions", "baseline")
    op.drop_column("growth_actions", "started_day")
    op.drop_column("growth_actions", "started_at")
