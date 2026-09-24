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
    timings: Mapped[dict] = mapped_column(JSON, default=dict)


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

class JobLease(Base):
    __tablename__ = "job_leases"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_bucket: Mapped[str | None] = mapped_column(String(32))


class ImportedReport(Base):
    __tablename__ = "imported_reports"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GrowthAssessment(Base):
    """Immutable observation-time inputs and descriptive regime for later evaluation."""
    __tablename__ = "growth_assessments"
    __table_args__ = (UniqueConstraint("video_id", "origin_at", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    origin_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[str] = mapped_column(String(64))
    features: Mapped[dict] = mapped_column(JSON)
    assessment: Mapped[dict] = mapped_column(JSON)


class ExperimentChange(Base):
    """Append-only user-reported changes; never writes to YouTube."""
    __tablename__ = "experiment_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id", ondelete="CASCADE"), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dimension: Mapped[str] = mapped_column(String(32))
    before_value: Mapped[str] = mapped_column(String(2000))
    after_value: Mapped[str] = mapped_column(String(2000))
    rationale: Mapped[str] = mapped_column(String(2000))


class PredictionAudit(Base):
    __tablename__ = "prediction_audits"
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecasts.id", ondelete="CASCADE"), primary_key=True)
    model_run_id: Mapped[int] = mapped_column(ForeignKey("model_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    feedback: Mapped[dict] = mapped_column(JSON, default=dict)
    recommendations: Mapped[list] = mapped_column(JSON)


class BackfillRun(Base):
    """Manual historical import runs; separate from hourly sync_runs."""
    __tablename__ = "backfill_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    issues: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class BackfillProgress(Base):
    """Resumable cursor per video and history kind; never rewinds committed work."""
    __tablename__ = "backfill_progress"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    first: Mapped[date] = mapped_column(Date)
    target: Mapped[date] = mapped_column(Date)
    through: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrafficDaily(Base):
    """Historical daily traffic sources; paid rows stay marked and excluded from organic learning."""
    __tablename__ = "video_traffic_daily"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    views: Mapped[int] = mapped_column(BigInteger)
    watch_minutes: Mapped[float] = mapped_column(Float)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BackfillReport(Base):
    """Historical period reports (retention per calendar month) with their exact window."""
    __tablename__ = "backfill_reports"
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    start: Mapped[date] = mapped_column(Date, primary_key=True)
    end: Mapped[date] = mapped_column(Date, primary_key=True)
    rows: Mapped[list] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningDataset(Base):
    """Reproducible historical dataset build: audit, exclusions and channel baselines at build time."""
    __tablename__ = "learning_datasets"
    signature: Mapped[str] = mapped_column(String(64), primary_key=True)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON)
    audit: Mapped[dict] = mapped_column(JSON)
    rows_per_horizon: Mapped[dict] = mapped_column(JSON)
    exclusions: Mapped[dict] = mapped_column(JSON)
    baselines: Mapped[dict] = mapped_column(JSON)


class LearningBacktest(Base):
    __tablename__ = "learning_backtests"
    __table_args__ = (UniqueConstraint("dataset_signature", "horizon_hours", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_signature: Mapped[str] = mapped_column(ForeignKey("learning_datasets.signature", ondelete="CASCADE"), index=True)
    horizon_hours: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(64))
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    champion: Mapped[str] = mapped_column(String(64))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict] = mapped_column(JSON)
    parameters: Mapped[dict] = mapped_column(JSON)
    signals: Mapped[list] = mapped_column(JSON)


class AnalyticsForecast(Base):
    """Analytics-view forecast with the exact features and model used; evaluated once the days are observed."""
    __tablename__ = "analytics_forecasts"
    __table_args__ = (UniqueConstraint("video_id", "origin_day", "horizon_hours"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    origin_day: Mapped[date] = mapped_column(Date, index=True)
    horizon_hours: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64))
    dataset_signature: Mapped[str | None] = mapped_column(String(64))
    predicted_views: Mapped[float] = mapped_column(Float)
    baseline_views: Mapped[float] = mapped_column(Float)
    lower_views: Mapped[float | None] = mapped_column(Float)
    upper_views: Mapped[float | None] = mapped_column(Float)
    interval_kind: Mapped[str] = mapped_column(String(32))
    features: Mapped[dict] = mapped_column(JSON)
    regime: Mapped[str] = mapped_column(String(32))
    actual_views: Mapped[int | None] = mapped_column(BigInteger)
    absolute_error: Mapped[float | None] = mapped_column(Float)
    log_error: Mapped[float | None] = mapped_column(Float)
    eligibility: Mapped[str | None] = mapped_column(String(64))
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyRecommendation(Base):
    __tablename__ = "strategy_recommendations"
    __table_args__ = (UniqueConstraint("video_id", "origin_day", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    origin_day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[str] = mapped_column(String(64))
    regime: Mapped[str] = mapped_column(String(32))
    recommendation: Mapped[dict] = mapped_column(JSON)
    forecast_ids: Mapped[list] = mapped_column(JSON, default=list)
    decision_ids: Mapped[list] = mapped_column(JSON, default=list)


class GrowthScore(Base):
    """V5 daily relative priority scores per video with explained components; no probabilities."""
    __tablename__ = "growth_scores"
    __table_args__ = (UniqueConstraint("video_id", "day", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    state: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    opportunity: Mapped[dict] = mapped_column(JSON)
    viewer: Mapped[dict] = mapped_column(JSON)
    subscriber: Mapped[dict] = mapped_column(JSON)
    revival: Mapped[dict] = mapped_column(JSON)
    momentum: Mapped[dict] = mapped_column(JSON, default=dict)


class GrowthAction(Base):
    """One recommended action per video at a time; scored later against observed analytics.

    Lifecycle: proposed -> running -> evaluated. A proposal is only a recommendation: the system
    cannot execute anything on YouTube, so nothing is measured and nothing is blocked until a
    human confirms the execution, which freezes the baseline and starts the measurement window.
    """
    __tablename__ = "growth_actions"
    __table_args__ = (UniqueConstraint("video_id", "created_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    created_day: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    target_metric: Mapped[str] = mapped_column(String(32))
    window_days: Mapped[int] = mapped_column(Integer)
    evaluate_after: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    # Confirmed execution by the channel owner; without it the row stays a proposal.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_day: Mapped[date | None] = mapped_column(Date)
    baseline: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    evaluation: Mapped[dict | None] = mapped_column(JSON)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GrowthPlan(Base):
    __tablename__ = "growth_plans"
    __table_args__ = (UniqueConstraint("day", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    plan: Mapped[dict] = mapped_column(JSON)


class DiscoveryRun(Base):
    """One budgeted, resumable external discovery pass; quota units are accounted per run and per day."""
    __tablename__ = "discovery_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    units_used: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class DiscoveryQuota(Base):
    __tablename__ = "discovery_quota"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    units: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelPlaylist(Base):
    """Own playlists as reported by the API. Absence of rows means unknown, never "none exist"."""
    __tablename__ = "channel_playlists"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    item_count: Mapped[int | None] = mapped_column(Integer)
    privacy: Mapped[str | None] = mapped_column(String(32))
    first_seen_day: Mapped[date] = mapped_column(Date)
    last_seen_day: Mapped[date] = mapped_column(Date, index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DiscoveryQuery(Base):
    """Search probe cache: each normalised query is probed at most once per REPROBE_DAYS."""
    __tablename__ = "discovery_queries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query: Mapped[str] = mapped_column(String(200), unique=True)
    source: Mapped[str] = mapped_column(String(32))
    seed_video_id: Mapped[str | None] = mapped_column(String(64))
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_probed_day: Mapped[date | None] = mapped_column(Date)
    next_probe_day: Mapped[date | None] = mapped_column(Date)
    probe_count: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[dict] = mapped_column(JSON, default=dict)


class DiscoveryItem(Base):
    """Public metadata of an external video seen via search probes or as a recommending source."""
    __tablename__ = "discovery_items"
    video_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    channel_title: Mapped[str | None] = mapped_column(String(256))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    views: Mapped[int | None] = mapped_column(BigInteger)
    likes: Mapped[int | None] = mapped_column(BigInteger)
    comments: Mapped[int | None] = mapped_column(BigInteger)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    via: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_day: Mapped[date] = mapped_column(Date)
    last_seen_day: Mapped[date] = mapped_column(Date)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)


class DiscoveryChannel(Base):
    __tablename__ = "discovery_channels"
    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    subscribers: Mapped[int | None] = mapped_column(BigInteger)
    video_count: Mapped[int | None] = mapped_column(Integer)
    views: Mapped[int | None] = mapped_column(BigInteger)
    first_seen_day: Mapped[date] = mapped_column(Date)
    last_seen_day: Mapped[date] = mapped_column(Date)


class DiscoverySignal(Base):
    """Own-analytics demand evidence per video: search terms, recommending videos, external URLs (windowed)."""
    __tablename__ = "discovery_signals"
    __table_args__ = (UniqueConstraint("video_id", "kind", "detail", "window_end"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(String(512))
    window_start: Mapped[date] = mapped_column(Date)
    window_end: Mapped[date] = mapped_column(Date)
    views: Mapped[int] = mapped_column(BigInteger)
    watch_minutes: Mapped[float] = mapped_column(Float)
    fetched_day: Mapped[date] = mapped_column(Date)


class DiscoveryOpportunity(Base):
    """Daily opportunity snapshot (discovery memory); evaluated later against own traffic signals."""
    __tablename__ = "discovery_opportunities"
    __table_args__ = (UniqueConstraint("day", "kind", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(200))
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"), index=True)
    gap: Mapped[str] = mapped_column(String(40))
    scores: Mapped[dict] = mapped_column(JSON)
    components: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="open")
    outcome: Mapped[str | None] = mapped_column(String(32))
    evaluation: Mapped[dict | None] = mapped_column(JSON)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
