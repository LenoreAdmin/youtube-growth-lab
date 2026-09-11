from datetime import datetime, timezone, date
from sqlalchemy import String, Integer, BigInteger, Float, DateTime, Date, JSON, ForeignKey, UniqueConstraint, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    subscribers: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("video_id", "observed_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    views: Mapped[int] = mapped_column(BigInteger)
    likes: Mapped[int | None] = mapped_column(BigInteger)
    comments: Mapped[int | None] = mapped_column(BigInteger)


class Daily(Base):
    __tablename__ = "video_daily"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    views: Mapped[int] = mapped_column(BigInteger)
    watch_minutes: Mapped[float] = mapped_column(Float)
    average_duration: Mapped[float] = mapped_column(Float)
    average_percentage: Mapped[float] = mapped_column(Float)
    subscribers_gained: Mapped[int] = mapped_column(Integer)
    subscribers_lost: Mapped[int] = mapped_column(Integer)
    likes: Mapped[int] = mapped_column(Integer)
    comments: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(64), default="UNKNOWN")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    """Retention/traffic/revenue preserve their exact date window and source."""
    __tablename__ = "reports"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    start: Mapped[date] = mapped_column(Date, primary_key=True)
    end: Mapped[date] = mapped_column(Date, primary_key=True)
    rows: Mapped[list] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Reach(Base):
    __tablename__ = "reach_daily"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    impressions: Mapped[int] = mapped_column(BigInteger)
    ctr: Mapped[float | None] = mapped_column(Float)
    report_id: Mapped[str] = mapped_column(String(128))


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    issues: Mapped[list] = mapped_column(JSON, default=list)


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (UniqueConstraint("video_id", "origin_at", "horizon_hours"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    origin_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    horizon_hours: Mapped[int] = mapped_column(Integer)
    origin_views: Mapped[int] = mapped_column(BigInteger)
    predicted_views: Mapped[float] = mapped_column(Float)
    lower_views: Mapped[float] = mapped_column(Float)
    upper_views: Mapped[float] = mapped_column(Float)
    p100k: Mapped[float | None] = mapped_column(Float)
    p1m: Mapped[float | None] = mapped_column(Float)
    features: Mapped[dict] = mapped_column(JSON)
    model_version: Mapped[str] = mapped_column(String(64))
    calibration_n: Mapped[int] = mapped_column(Integer)
    actual_views: Mapped[int | None] = mapped_column(BigInteger)
    actual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    absolute_error: Mapped[float | None] = mapped_column(Float)


class ModelRun(Base):
    __tablename__ = "model_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    horizon_hours: Mapped[int] = mapped_column(Integer)
    training_rows: Mapped[int] = mapped_column(Integer)
    parameters: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)


class Experiment(Base):
    """Extension seam; V1 never automatically publishes or manipulates engagement."""
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    hypothesis: Mapped[str] = mapped_column(String(2000))
    strategy: Mapped[str] = mapped_column(String(64))
    arms: Mapped[list] = mapped_column(JSON)
    objective_weights: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="draft")


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    hypothesis: Mapped[str] = mapped_column(String(2000))
    expected_result: Mapped[dict] = mapped_column(JSON)
    content_strategy: Mapped[str] = mapped_column(String(1000))
    title_strategy: Mapped[str] = mapped_column(String(1000))
    thumbnail_strategy: Mapped[str] = mapped_column(String(1000))
    hook_strategy: Mapped[str] = mapped_column(String(1000))
    audience: Mapped[str] = mapped_column(String(1000))
    strategy_key: Mapped[str] = mapped_column(String(64), index=True)
    design: Mapped[str] = mapped_column(String(32))
    control_video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    confounders: Mapped[str] = mapped_column(String(2000))
    measurement: Mapped[dict | None] = mapped_column(JSON)
    actual_result: Mapped[dict | None] = mapped_column(JSON)
    deviation: Mapped[float | None] = mapped_column(Float)
    suspected_cause: Mapped[str | None] = mapped_column(String(2000))
    confidence_score: Mapped[float | None] = mapped_column(Float)
    next_hypothesis: Mapped[str | None] = mapped_column(String(2000))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryReview(Base):
    __tablename__ = "memory_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    suspected_cause: Mapped[str] = mapped_column(String(2000))
    next_hypothesis: Mapped[str] = mapped_column(String(2000))


class IngestCursor(Base):
    __tablename__ = "ingest_cursors"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    through: Mapped[date] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
