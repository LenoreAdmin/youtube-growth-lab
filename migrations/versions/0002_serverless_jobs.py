"""Durable leases and reporting receipts; additive, preserves existing data."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade():
    op.create_table("job_leases",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_bucket", sa.String(32), nullable=True))
    op.create_table("imported_reports",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("imported_reports")
    op.drop_table("job_leases")
