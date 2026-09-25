"""Die externe Web-Suche ist verworfen: der eigene Zaehler dafuer wird nicht mehr gebraucht."""
from alembic import op
import sqlalchemy as sa
revision = "0015"
down_revision = "0014"


def upgrade():
    op.drop_table("web_search_quota")


def downgrade():
    op.create_table("web_search_quota",
                    sa.Column("day", sa.Date(), primary_key=True),
                    sa.Column("queries", sa.Integer(), nullable=False, server_default="0"),
                    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
