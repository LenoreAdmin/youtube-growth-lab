"""Reproducible historical training data from retrospectively loaded analytics.

Leakage rules: a training origin R (Pacific day) may only use analytics rows whose
day is at or before R - lag, because that is when Google actually served them.
Snapshot-derived V2/V3 features never enter historical rows: they did not exist
before the first live sync. Targets are analytics views on the days after R.
Rows never mix paid periods into organic learning; the exclusion reason is kept.
"""
import hashlib
import json
from math import log1p
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .config import settings
from .models import Video, Daily, TrafficDaily, BackfillReport, Reach, IngestCursor
from .metrics import aware

VERSION = "history-v4"
HORIZONS = {24: 1, 168: 7, 720: 30}
MIN_HISTORY_DAYS = 28
PAID_WINDOW_DAYS = 32
PAID_SOURCES = {"ADVERTISING", "PROMOTED"}
TRAFFIC_GROUPS = {
    "search": {"YT_SEARCH"}, "suggested": {"RELATED_VIDEO"}, "external": {"EXT_URL"},
    "browse": {"YT_CHANNEL", "SUBSCRIBER", "NOTIFICATION", "END_SCREEN", "PLAYLIST", "YT_PLAYLIST_PAGE", "YT_OTHER_PAGE"},
    "shorts": {"SHORTS"},
}
FEATURES = ["log_views_7d", "log_views_28d", "velocity_7d", "ratio_7_28", "accel_7d", "days_above_28d_last3",
            "watch_minutes_7d", "avd_7d", "pct_7d", "subscriber_conversion_7d", "subs_net_7d", "like_rate_7d",
            "comment_rate_7d", "traffic_search", "traffic_suggested", "traffic_external", "traffic_browse",
            "traffic_shorts", "retention_avg", "ctr_7d", "age_days", "duration_seconds", "upload_weekday", "upload_hour"]


def lag_days():
    return max(2, settings.analytics_lag_days)


def pacific_day(moment):
    return aware(moment).astimezone(ZoneInfo("America/Los_Angeles")).date()


class VideoHistory:
    """In-memory per-video history; all lookups are by Pacific calendar day."""
    def __init__(self, video, daily, traffic, retention, reach, covered_through):
        self.video = video
        self.daily = {d.day: d for d in daily}
        self.traffic = defaultdict(dict)
        for row in traffic:
            self.traffic[row.day][row.source] = (row.views, bool(row.paid) or row.source in PAID_SOURCES)
        self.retention = sorted(retention, key=lambda r: r.end)
        self.reach = {r.day: r for r in reach}
        self.first_day = min(self.daily) if self.daily else None
        self.last_day = max(self.daily) if self.daily else None
        self.covered_through = covered_through

    def views(self, day):
        row = self.daily.get(day)
        return row.views if row else 0

    def paid_views(self, start, end):
        total = 0
        for offset in range((end-start).days+1):
            for views, paid in self.traffic.get(start+timedelta(days=offset), {}).values():
                if paid:
                    total += views
        return total


def load(session):
    videos = list(session.scalars(select(Video).where(Video.active.is_(True))))
    result = []
    for v in videos:
        cursor = session.get(IngestCursor, (v.id, "daily"))
        result.append(VideoHistory(v,
            list(session.scalars(select(Daily).where(Daily.video_id == v.id))),
            list(session.scalars(select(TrafficDaily).where(TrafficDaily.video_id == v.id))),
            list(session.scalars(select(BackfillReport).where(BackfillReport.video_id == v.id, BackfillReport.kind == "retention"))),
            list(session.scalars(select(Reach).where(Reach.video_id == v.id))),
            cursor.through if cursor else None))
    return result


def _window(history, end, days):
    return [end-timedelta(days=i) for i in range(days-1, -1, -1)]


def _weighted(rows, attribute):
    total = sum(r.views for r in rows)
    return sum(getattr(r, attribute)*r.views for r in rows)/total if total else None


def features_at(history, origin, lag=None):
    """Features known at real time `origin` (Pacific day). None when history is too short."""
    lag = lag_days() if lag is None else lag
    known_end = origin-timedelta(days=lag)
    if history.first_day is None or history.first_day > known_end-timedelta(days=MIN_HISTORY_DAYS-1):
        return None
    if history.last_day is not None and known_end > history.last_day:
        known_end = min(known_end, history.last_day)  # Never pretend later days are known.
    w7, w28 = _window(history, known_end, 7), _window(history, known_end, 28)
    prev7 = _window(history, known_end-timedelta(days=7), 7)
    rows7 = [history.daily[d] for d in w7 if d in history.daily]
    v7 = sum(history.views(d) for d in w7)
    v28 = sum(history.views(d) for d in w28)
    vprev = sum(history.views(d) for d in prev7)
    mean7, mean28, meanprev = v7/7, v28/28, vprev/7
    watch = sum(r.watch_minutes for r in rows7)
    gained = sum(r.subscribers_gained for r in rows7)
    lost = sum(r.subscribers_lost for r in rows7)
    likes = sum(r.likes for r in rows7)
    comments = sum(r.comments for r in rows7)
    shares = {}
    totals = defaultdict(int)
    any_traffic = False
    for d in w7:
        for source, (views, _) in history.traffic.get(d, {}).items():
            any_traffic = True
            totals[source] += views
    traffic_total = sum(totals.values())
    for group, names in TRAFFIC_GROUPS.items():
        shares[f"traffic_{group}"] = sum(totals[s] for s in names)/traffic_total if any_traffic and traffic_total else None
    retention = next((r for r in reversed(history.retention) if r.end <= known_end and r.rows), None)
    retention_avg = None
    if retention:
        pairs = sorted((r["elapsedVideoTimeRatio"], r["audienceWatchRatio"]) for r in retention.rows
                       if "elapsedVideoTimeRatio" in r and "audienceWatchRatio" in r)
        width = pairs[-1][0]-pairs[0][0] if len(pairs) > 1 else 0
        retention_avg = sum((b[0]-a[0])*(a[1]+b[1])/2 for a, b in zip(pairs, pairs[1:]))/width if width > 0 else None
    reach = [history.reach[d] for d in w7 if d in history.reach and history.reach[d].ctr is not None]
    impressions = sum(r.impressions for r in reach)
    published = aware(history.video.published_at)
    return {
        "origin": str(origin), "known_end": str(known_end), "lag_days": lag,
        "source": "analytics_daily_retrospective",
        "log_views_7d": log1p(v7), "log_views_28d": log1p(v28), "views_7d": v7, "views_28d": v28,
        "velocity_7d": mean7, "ratio_7_28": mean7/mean28 if mean28 > 0 else None,
        "accel_7d": (mean7-meanprev)/max(1.0, meanprev),
        "days_above_28d_last3": sum(history.views(d) > mean28 for d in w7[-3:]),
        "watch_minutes_7d": watch, "avd_7d": _weighted(rows7, "average_duration"), "pct_7d": _weighted(rows7, "average_percentage"),
        "subscriber_conversion_7d": gained/v7 if v7 else None, "subs_gained_7d": gained, "subs_net_7d": gained-lost,
        "like_rate_7d": likes/v7 if v7 else None, "comment_rate_7d": comments/v7 if v7 else None,
        **shares, "traffic_total_7d": traffic_total if any_traffic else None,
        "retention_avg": retention_avg, "retention_window": [str(retention.start), str(retention.end)] if retention else None,
        "ctr_7d": sum(r.impressions*r.ctr for r in reach)/impressions if impressions else None,
        "paid_views_32d": history.paid_views(known_end-timedelta(days=PAID_WINDOW_DAYS-1), known_end),
        # Ads inside the analytics lag gap are unknown to Google's report but known to the channel owner.
        "paid_views_lag_gap": history.paid_views(known_end+timedelta(days=1), origin) if origin > known_end else 0,
        "age_days": (origin-pacific_day(published)).days, "duration_seconds": history.video.duration_seconds,
        "upload_weekday": published.weekday(), "upload_hour": published.hour,
        "content_type": next((r.content_type for r in rows7 if r.content_type != "UNKNOWN"), "UNKNOWN"),
        "analytics_days_28d": sum(d in history.daily for d in w28),
    }


def target_at(history, origin, horizon_hours):
    """Analytics views on the days after origin; None until every target day is observed."""
    days = HORIZONS[horizon_hours]
    start, end = origin+timedelta(days=1), origin+timedelta(days=days)
    if history.last_day is None or end > history.last_day:
        return None
    total = sum(history.views(start+timedelta(days=i)) for i in range(days))
    return {"views": total, "paid_views": history.paid_views(start, end), "start": str(start), "end": str(end),
            "observed_days": sum(start+timedelta(days=i) in history.daily for i in range(days))}


def build(histories, horizon_hours, lag=None, spacing_days=1, last_origin=None):
    """Deterministic rows for one horizon. Excluded rows keep their reason for audit."""
    lag = lag_days() if lag is None else lag
    rows, excluded = [], defaultdict(int)
    for history in histories:
        if history.first_day is None:
            excluded["no_analytics"] += 1
            continue
        origin = history.first_day+timedelta(days=MIN_HISTORY_DAYS-1+lag)
        stop = history.last_day-timedelta(days=HORIZONS[horizon_hours])
        if last_origin:
            stop = min(stop, last_origin)
        while origin <= stop:
            f = features_at(history, origin, lag)
            t = target_at(history, origin, horizon_hours)
            reason = None
            if f is None:
                reason = "insufficient_history"
            elif t is None:
                reason = "target_unobserved"
            elif f["paid_views_32d"] > 0:
                reason = "paid_feature_window"
            elif f["paid_views_lag_gap"] > 0:
                reason = "paid_lag_gap"
            elif t["paid_views"] > 0:
                reason = "paid_target_window"
            elif f["analytics_days_28d"] == 0:
                reason = "missing_analytics"
            if reason:
                excluded[reason] += 1
            else:
                rows.append({"video_id": history.video.id, "origin": origin, "horizon_hours": horizon_hours, "features": f,
                             "target_views": t["views"], "target_ratio": (t["views"]/HORIZONS[horizon_hours])/max(1.0, f["velocity_7d"]),
                             "target": t})
            origin += timedelta(days=spacing_days)
    return rows, dict(excluded)


def sample_sizes(rows):
    """Rows, distinct origin days and distinct videos are three different things; never conflate them."""
    per_video = {}
    for r in rows:
        per_video[r["video_id"]] = per_video.get(r["video_id"], 0)+1
    largest = max(per_video.values()) if per_video else 0
    return {"n_rows": len(rows), "n_origins": len({r["origin"] for r in rows}), "n_videos": len(per_video),
            "rows_per_video": per_video, "largest_video_share": largest/len(rows) if rows else None,
            "independence_note": "Tageszeilen desselben Videos sind korreliert; n_videos ist die Anzahl unabhängiger Einheiten."}


def signature(rows, config):
    digest = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode())
    for r in rows:
        digest.update(f"{r['video_id']}|{r['origin']}|{r['horizon_hours']}|{r['target_views']}".encode())
    return digest.hexdigest()


def vector(f):
    return [float(f.get(k) or 0) for k in FEATURES]+[float(f.get(k) is None) for k in FEATURES]


def audit(histories, lag=None):
    """Per-video sample sizes, paid periods and gaps; the honest basis for every claim."""
    lag = lag_days() if lag is None else lag
    report = []
    for h in histories:
        paid_days = sorted(d for d, sources in h.traffic.items() if any(p and v > 0 for v, p in sources.values()))
        span = (h.last_day-h.first_day).days+1 if h.first_day else 0
        report.append({"video_id": h.video.id, "first_day": str(h.first_day) if h.first_day else None,
            "last_day": str(h.last_day) if h.last_day else None, "analytics_days": len(h.daily),
            "missing_days_in_span": max(0, span-len(h.daily)), "traffic_days": len(h.traffic),
            "paid_days": len(paid_days), "paid_first": str(paid_days[0]) if paid_days else None,
            "paid_last": str(paid_days[-1]) if paid_days else None,
            "retention_windows": len(h.retention), "reach_days": len(h.reach),
            "snapshot_features_available_from": "first live sync only"})
    return {"videos": report, "lag_days": lag, "n_videos": len(histories), "version": VERSION,
            "excluded_feature_sources": ["snapshots (velocity/acceleration from live counters)", "growth_assessments (V2)",
                                        "forecasts/prediction_audits (V3)", "reach_daily before reporting job creation"]}
