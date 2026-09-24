"""Daily counter for public web searches: the free provider tier must never be exceeded."""
from alembic import op
import sqlalchemy as sa
revision = "0013"
down_revision = "0012"


def upgrade():
    op.create_table("web_search_quota",
                    sa.Column("day", sa.Date(), primary_key=True),
                    sa.Column("queries", sa.Integer(), nullable=False, server_default="0"),
                    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("web_search_quota")
