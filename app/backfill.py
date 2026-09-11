"""Historical import of analytics that Google still serves; never invents observation points.

Snapshots remain real observation times only. History lands in separate tables
(video_traffic_daily, backfill_reports) or fills days missing before the earliest
existing video_daily row. Progress cursors make interrupted runs resumable and
repeated runs free of duplicates. Existing production rows are never deleted or
overwritten.
"""
import logging
from calendar import monthrange
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, func
from googleapiclient.errors import HttpError
from .config import settings
from .db import Session
from .models import Video, Daily, Reach, IngestCursor, BackfillRun, BackfillProgress, TrafficDaily, BackfillReport, utcnow
from .metrics import aware
from .budget import Budget, SyncBudgetExceeded
from .jobs import acquire, release
from .pipeline import METRICS
from .youtube import YouTube

log = logging.getLogger(__name__)
LEASE = "youtube-backfill"
KINDS = ("daily", "traffic", "retention")
DAILY_CHUNK = 179
TRAFFIC_CHUNK = 90  # About ten sources per day; keeps paging per chunk small.
MAX_ATTEMPTS = 5
SYNC_MARGIN = 45  # Days below the sync cursor reserved for the hourly refresh window (30 days plus slack).
PAID_SOURCE = "ADVERTISING"
LIMITS = [
    "Analytics API: Tageswerte, Traffic-Quellen und Retention rückwirkend bis zum Upload-Datum, soweit Google sie liefert (Lag ANALYTICS_LAG_DAYS).",
    "Reporting API (Impressionen/CTR): Google erzeugt historische Berichte nur für die 30 Tage vor Erstellung des Reporting-Jobs; der stündliche Sync importiert sie, ältere Reichweite ist über keine API verfügbar.",
    "Tageswerte innerhalb des Sync-Fensters (letzte 45 Tage vor dem Sync-Cursor) und Videos ohne ersten Sync bleiben dem stündlichen Sync vorbehalten.",
    "Snapshots (kumulative Zähler) sind nur echte Beobachtungszeitpunkte; historische Zählerstände werden nicht rekonstruiert.",
    "Retention ist nur je Zeitraum abrufbar (hier Kalendermonate, Pacific Time), nicht tagesgenau.",
    "Returning Viewers und Zuschauer-Lifetime-Value liefern die verwendeten APIs nicht.",
]


class Throttled(Exception):
    """Quota or upstream availability limit; retry later, do not mark work as failed."""


def classify(exc):
    if isinstance(exc, HttpError):
        status = getattr(exc, "status_code", None)
        reason = str(getattr(exc, "reason", "") or "").lower().replace(" ", "")
        if status == 429 or (status is not None and status >= 500) or "quota" in reason or "ratelimit" in reason:
            return "throttled"
        if status in (400, 404):
            return "unsupported"
        if status in (401, 403):
            return "forbidden"
        return "error"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "transient"
    return "error"


def pacific_day(moment):
    return aware(moment).astimezone(ZoneInfo("America/Los_Angeles")).date()


def analytics_end(now):
    return pacific_day(now)-timedelta(days=max(2, settings.analytics_lag_days))


def upsert(session, model):
    """Dialect-native INSERT ... ON CONFLICT: atomic and idempotent under concurrent writers."""
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model.__table__)


def sync_window_start(session, video_id, first):
    """Earliest day the hourly sync may delete and rewrite; None if it has never processed the video."""
    cursor = session.get(IngestCursor, (video_id, "daily"))
    last = cursor.through if cursor else session.scalar(select(func.max(Daily.day)).where(Daily.video_id == video_id))
    return max(first, last-timedelta(days=30)) if last else None


def month_windows(first, end):
    """Complete calendar months from the month of `first` through the last one ending at or before `end`."""
    year, month = first.year, first.month
    while True:
        start = date(year, month, 1)
        stop = date(year, month, monthrange(year, month)[1])
        if stop > end:
            return
        yield max(first, start), stop
        year, month = (year+1, 1) if month == 12 else (year, month+1)


def run(client=None, retry_failed=False):
    owner, skipped = acquire(Session, None, LEASE)
    if skipped:
        return {"status": skipped}
    try:
        return _run(client, retry_failed)
    finally:
        release(Session, owner, None, LEASE)


def _run(client, retry_failed):
    budget = Budget(settings.sync_budget_seconds)
    issues, now, stats = [], utcnow(), {"daily_rows": 0, "traffic_rows": 0, "retention_windows": 0, "requests": 0}
    status = "ok"
    with Session() as s:
        run_row = BackfillRun()
        s.add(run_row)
        s.commit()
        run_id = run_row.id
    try:
        client = client or YouTube(budget=budget)
        end = analytics_end(now)
        with Session() as s:
            if retry_failed:
                for row in s.scalars(select(BackfillProgress).where(BackfillProgress.status == "error")):
                    row.status, row.attempts, row.note = "pending", 0, None
                s.commit()
            videos = list(s.scalars(select(Video).where(Video.active.is_(True))))
            attempts = {(p.video_id, p.kind): p.attempts for p in s.scalars(select(BackfillProgress))}
        # Reach/CTR reports are imported only by the hourly sync: one writer per table, no shared inserts.
        # Focus video first, then videos with the fewest prior failed attempts, newest uploads first.
        ordered = sorted(videos, key=lambda v: (v.id != settings.focus_video_id,
            max(attempts.get((v.id, k), 0) for k in KINDS), -aware(v.published_at).timestamp()))
        for video in ordered:
            for kind in KINDS:
                budget.check()
                try:
                    _stage(client, video.id, kind, end, now, budget, stats)
                except SyncBudgetExceeded:
                    raise
                except Throttled:
                    raise
                except Exception as exc:
                    issues.append(f"{video.id}/{kind}: {type(exc).__name__}")
    except SyncBudgetExceeded:
        status = "deferred"
        issues.append("Zeitbudget erreicht; gespeicherter Fortschritt wird beim nächsten Backfill-Lauf fortgesetzt.")
    except Throttled:
        status = "throttled"
        issues.append("API-Limit oder Google-Störung; Fortschritt gespeichert, später erneut starten.")
    except Exception as exc:
        status = "failed"
        issues.append(f"backfill: {type(exc).__name__}")
        log.error("Backfill failed (%s); secrets and raw API responses omitted", type(exc).__name__)
    if status == "ok" and any(not i.startswith("Zeitbudget") for i in issues):
        status = "partial"
    with Session() as s:
        run_row = s.get(BackfillRun, run_id)
        run_row.finished_at, run_row.status, run_row.issues, run_row.stats = utcnow(), status, issues, stats
        s.commit()
    return {"status": status, "issues": issues, "stats": stats}


def _stage(client, video_id, kind, end, now, budget, stats):
    with Session() as s:
        video = s.get(Video, video_id)
        first = max(date(2009, 1, 1), pacific_day(video.published_at))
        progress = s.get(BackfillProgress, (video_id, kind))
        if progress is None:
            progress = BackfillProgress(video_id=video_id, kind=kind, first=first, target=end, through=None,
                                        status="pending", attempts=0)
            s.add(progress)
        if progress.status == "error":
            return  # Stays skipped until an explicit retry_failed run resets it.
        target = end
        if kind == "daily":
            if progress.status == "complete":
                return  # One-time gap filler; the hourly sync owns everything after its earliest row.
            window = sync_window_start(s, video_id, first)
            if window is None:
                # The sync fetches the whole history itself on its first pass; never race it.
                progress.status, progress.note, progress.updated_at = "pending", "Wartet auf ersten Sync des Videos", now
                s.commit()
                return
            # Stay strictly below the sync's rewrite window, and below rows it already owns.
            target = min(end, window-timedelta(days=SYNC_MARGIN-30))
            earliest = s.scalar(select(func.min(Daily.day)).where(Daily.video_id == video_id))
            if earliest is not None and progress.through is None:
                target = min(target, earliest-timedelta(days=1))
        elif kind == "retention":
            windows = list(month_windows(first, end))
            target = windows[-1][1] if windows else first-timedelta(days=1)
        progress.target = target
        start = max(first, progress.through+timedelta(days=1)) if progress.through else first
        if start > target:
            if progress.status != "complete":
                progress.status = "complete"
                if kind == "daily" and progress.through is None:
                    progress.note = "Bereits durch Sync abgedeckt"
            progress.updated_at = now
            s.commit()
            return
        progress.status, progress.updated_at = "partial", now
        s.commit()
        try:
            if kind == "daily":
                _daily(s, client, video, start, target, now, budget, stats)
            elif kind == "traffic":
                _traffic(s, client, video, start, target, now, budget, stats)
            else:
                _retention(s, client, video, start, target, now, budget, stats)
            progress = s.get(BackfillProgress, (video_id, kind))
            progress.status, progress.note, progress.updated_at = "complete", None, utcnow()
            s.commit()
        except SyncBudgetExceeded:
            s.rollback()
            raise
        except Exception as exc:
            s.rollback()
            failure = classify(exc)
            if failure == "throttled":
                raise Throttled() from exc
            progress = s.get(BackfillProgress, (video_id, kind))
            progress.attempts += 1
            progress.status = "error" if progress.attempts >= MAX_ATTEMPTS or failure in ("unsupported", "forbidden") else "partial"
            progress.note = f"{failure}: {type(exc).__name__}"
            progress.updated_at = utcnow()
            s.commit()
            raise


def _advance(session, video_id, kind, through):
    progress = session.get(BackfillProgress, (video_id, kind))
    progress.through, progress.updated_at = through, utcnow()


def _daily(s, client, video, start, target, now, budget, stats):
    content_type = s.scalar(select(Daily.content_type).where(Daily.video_id == video.id).limit(1)) or "UNKNOWN"
    while start <= target:
        budget.check()
        stop = min(target, start+timedelta(days=DAILY_CHUNK))
        rows = client.query(video.id, start, stop, METRICS, "day")
        stats["requests"] += 1
        for row in rows:
            # Existing rows (sync-owned or concurrently inserted) are never touched: DO NOTHING is atomic.
            statement = upsert(s, Daily).values(video_id=video.id, day=date.fromisoformat(row["day"]), views=row["views"],
                watch_minutes=row["estimatedMinutesWatched"], average_duration=row["averageViewDuration"],
                average_percentage=row["averageViewPercentage"], subscribers_gained=row["subscribersGained"],
                subscribers_lost=row["subscribersLost"], likes=row["likes"], comments=row["comments"],
                content_type=content_type, fetched_at=now).on_conflict_do_nothing(index_elements=["video_id", "day"])
            stats["daily_rows"] += max(0, s.execute(statement).rowcount)
        _advance(s, video.id, "daily", stop)
        s.commit()
        start = stop+timedelta(days=1)


def _traffic(s, client, video, start, target, now, budget, stats):
    while start <= target:
        budget.check()
        stop = min(target, start+timedelta(days=TRAFFIC_CHUNK))
        rows = client.query(video.id, start, stop, "views,estimatedMinutesWatched", "day,insightTrafficSourceType")
        stats["requests"] += 1
        for row in rows:
            source = str(row["insightTrafficSourceType"])
            views = int(row.get("views", 0))
            if views < 0:
                raise ValueError("Invalid traffic metric")
            statement = upsert(s, TrafficDaily).values(video_id=video.id, day=date.fromisoformat(row["day"]), source=source,
                views=views, watch_minutes=float(row.get("estimatedMinutesWatched", 0) or 0), paid=source == PAID_SOURCE, fetched_at=now)
            s.execute(statement.on_conflict_do_update(index_elements=["video_id", "day", "source"],
                set_={k: statement.excluded[k] for k in ("views", "watch_minutes", "paid", "fetched_at")}))
            stats["traffic_rows"] += 1
        _advance(s, video.id, "traffic", stop)
        s.commit()
        start = stop+timedelta(days=1)


def _retention(s, client, video, start, target, now, budget, stats):
    for begin, finish in month_windows(start, target):
        budget.check()
        rows = client.query(video.id, begin, finish, "audienceWatchRatio,relativeRetentionPerformance", "elapsedVideoTimeRatio")
        stats["requests"] += 1
        statement = upsert(s, BackfillReport).values(video_id=video.id, kind="retention", start=begin, end=finish, rows=rows, fetched_at=now)
        s.execute(statement.on_conflict_do_update(index_elements=["video_id", "kind", "start", "end"],
            set_={"rows": statement.excluded.rows, "fetched_at": statement.excluded.fetched_at}))
        stats["retention_windows"] += 1
        _advance(s, video.id, "retention", finish)
        s.commit()


def summary(session):
    """Dashboard status: last run, per-kind progress counts and real coverage of stored history."""
    last = session.scalar(select(BackfillRun).order_by(BackfillRun.id.desc()))
    active = set(session.scalars(select(Video.id).where(Video.active.is_(True))))
    stages = {kind: {"complete": 0, "partial": 0, "pending": 0, "error": 0} for kind in KINDS}
    for row in session.scalars(select(BackfillProgress)):
        if row.video_id in active and row.kind in stages:
            stages[row.kind][row.status if row.status in stages[row.kind] else "pending"] += 1
    for kind in KINDS:
        stages[kind]["pending"] += len(active)-sum(stages[kind].values())
    daily = session.execute(select(func.min(Daily.day), func.max(Daily.day), func.count()).select_from(Daily)).one()
    traffic = session.execute(select(func.min(TrafficDaily.day), func.max(TrafficDaily.day)).select_from(TrafficDaily)).one()
    traffic_days = session.scalar(select(func.count()).select_from(
        select(TrafficDaily.video_id, TrafficDaily.day).distinct().subquery()))
    paid_days = session.scalar(select(func.count()).select_from(
        select(TrafficDaily.video_id, TrafficDaily.day).where(TrafficDaily.paid.is_(True), TrafficDaily.views > 0).distinct().subquery()))
    retention = session.scalar(select(func.count()).select_from(BackfillReport).where(BackfillReport.kind == "retention"))
    reach = session.execute(select(func.min(Reach.day), func.max(Reach.day), func.count()).select_from(Reach)).one()
    return {
        "last_run": {"id": last.id, "started_at": last.started_at, "finished_at": last.finished_at,
                     "status": last.status, "issues": last.issues, "stats": last.stats} if last else None,
        "videos": len(active),
        "stages": stages,
        "complete": bool(active) and all(stages[k]["complete"] == len(active) for k in KINDS),
        "coverage": {"daily_first": daily[0], "daily_last": daily[1], "daily_rows": daily[2],
                     "traffic_first": traffic[0], "traffic_last": traffic[1], "traffic_days": traffic_days,
                     "paid_days": paid_days, "retention_windows": retention,
                     "reach_first": reach[0], "reach_last": reach[1], "reach_days": reach[2]},
        "limits": LIMITS,
    }


def detail(session):
    rows = list(session.scalars(select(BackfillProgress).order_by(BackfillProgress.video_id, BackfillProgress.kind)))
    return [{"video_id": r.video_id, "kind": r.kind, "first": r.first, "target": r.target, "through": r.through,
             "status": r.status, "attempts": r.attempts, "note": r.note, "updated_at": r.updated_at} for r in rows]


def video_history(session, video_id):
    """Per-video history for the detail view; totals per source keep paid traffic visibly separate."""
    totals = {}
    for row in session.scalars(select(TrafficDaily).where(TrafficDaily.video_id == video_id)):
        entry = totals.setdefault(row.source, {"source": row.source, "views": 0, "watch_minutes": 0.0, "paid": row.paid})
        entry["views"] += row.views
        entry["watch_minutes"] += row.watch_minutes
    span = session.execute(select(func.min(TrafficDaily.day), func.max(TrafficDaily.day))
                           .where(TrafficDaily.video_id == video_id)).one()
    retention = []
    for report in session.scalars(select(BackfillReport).where(BackfillReport.video_id == video_id,
            BackfillReport.kind == "retention").order_by(BackfillReport.start)):
        pairs = sorted((r["elapsedVideoTimeRatio"], r["audienceWatchRatio"]) for r in report.rows
                       if "elapsedVideoTimeRatio" in r and "audienceWatchRatio" in r)
        width = pairs[-1][0]-pairs[0][0] if len(pairs) > 1 else 0
        average = sum((b[0]-a[0])*(a[1]+b[1])/2 for a, b in zip(pairs, pairs[1:]))/width if width > 0 else None
        retention.append({"start": report.start, "end": report.end, "average_watch_ratio": average, "points": len(pairs)})
    progress = [p for p in detail(session) if p["video_id"] == video_id]
    return {"traffic_sources": sorted(totals.values(), key=lambda t: -t["views"]),
            "traffic_first": span[0], "traffic_last": span[1],
            "paid_views": sum(t["views"] for t in totals.values() if t["paid"]),
            "retention_months": retention, "progress": progress}
