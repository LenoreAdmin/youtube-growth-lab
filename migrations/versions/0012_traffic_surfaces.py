"""Traffic surfaces: concrete places where an audience already exists, plus lever/source on actions.

The unique constraint on growth_actions gains the version, so the acquisition engine can propose an
action for the same video and day as the measurement engine without colliding with it.
"""
from alembic import op
import sqlalchemy as sa
revision = "0012"
down_revision = "0011"


def upgrade():
    op.create_table("traffic_surfaces",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("day", sa.Date(), nullable=False),
                    sa.Column("kind", sa.String(32), nullable=False),
                    sa.Column("key", sa.String(300), nullable=False),
                    sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="SET NULL"), nullable=True),
                    sa.Column("title", sa.String(300), nullable=False),
                    sa.Column("url", sa.String(500), nullable=True),
                    sa.Column("traffic_source", sa.String(32), nullable=False),
                    sa.Column("lever_class", sa.String(32), nullable=False),
                    sa.Column("evidence", sa.JSON(), nullable=False),
                    sa.Column("scores", sa.JSON(), nullable=False),
                    sa.Column("access", sa.JSON(), nullable=False),
                    sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
                    sa.Column("http_status", sa.Integer(), nullable=True),
                    sa.Column("status", sa.String(32), nullable=False, server_default="open"),
                    sa.UniqueConstraint("day", "kind", "key", name="uq_traffic_surfaces_day_kind_key"))
    op.create_index("ix_traffic_surfaces_day", "traffic_surfaces", ["day"])
    op.create_index("ix_traffic_surfaces_video_id", "traffic_surfaces", ["video_id"])
    for column in ("lever_class", "traffic_source"):
        op.add_column("growth_actions", sa.Column(column, sa.String(32), nullable=True))
    op.add_column("growth_actions", sa.Column("surface_key", sa.String(300), nullable=True))
    # Vorhandene Zeilen stammen aus der Messinfrastruktur und sind interne Verlinkung.
    op.execute("UPDATE growth_actions SET lever_class = 'internal_link' WHERE lever_class IS NULL")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE growth_actions DROP CONSTRAINT IF EXISTS growth_actions_video_id_created_day_key")
        op.create_unique_constraint("uq_growth_actions_video_day_version", "growth_actions",
                                    ["video_id", "created_day", "version"])
    else:
        # SQLite kennt kein ALTER CONSTRAINT: die Tabelle wird mit der neuen Eindeutigkeit neu aufgebaut.
        with op.batch_alter_table("growth_actions", copy_from=_growth_actions(), recreate="always"):
            pass
        inspector = sa.inspect(op.get_bind())
        if "ix_growth_actions_video_id" not in {i["name"] for i in inspector.get_indexes("growth_actions")}:
            op.create_index("ix_growth_actions_video_id", "growth_actions", ["video_id"])


def _growth_actions():
    """Der Stand der Tabelle nach dieser Migration – eingefroren, damit spätere Modelländerungen sie nicht verbiegen."""
    meta = sa.MetaData()
    return sa.Table("growth_actions", meta,
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("video_id", sa.String(64), sa.ForeignKey("videos.id", ondelete="CASCADE"),
                              nullable=False, index=True),
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
                    sa.Column("lever_class", sa.String(32), nullable=True),
                    sa.Column("traffic_source", sa.String(32), nullable=True),
                    sa.Column("surface_key", sa.String(300), nullable=True),
                    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
                    sa.Column("started_day", sa.Date(), nullable=True),
                    sa.Column("baseline", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
                    sa.UniqueConstraint("video_id", "created_day", "version",
                                        name="uq_growth_actions_video_day_version"))


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("uq_growth_actions_video_day_version", "growth_actions", type_="unique")
        op.create_unique_constraint("growth_actions_video_id_created_day_key", "growth_actions",
                                    ["video_id", "created_day"])
    op.drop_column("growth_actions", "surface_key")
    op.drop_column("growth_actions", "traffic_source")
    op.drop_column("growth_actions", "lever_class")
    op.drop_index("ix_traffic_surfaces_video_id", table_name="traffic_surfaces")
    op.drop_index("ix_traffic_surfaces_day", table_name="traffic_surfaces")
    op.drop_table("traffic_surfaces")
