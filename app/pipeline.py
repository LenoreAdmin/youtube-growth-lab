from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
from sqlalchemy import select, func, text, delete
import isodate
from .config import settings
from .db import Session, engine
from .models import Channel, Video, Snapshot, Daily, Report, Reach, SyncRun, Forecast, IngestCursor, utcnow
from .metrics import features, momentum, aware
from .prediction import mature, predict
from .youtube import YouTube

log = logging.getLogger(__name__)
METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost,likes,comments"


def latest_features(session, now=None):
    now = now or utcnow()
    result = []
    for v in session.scalars(select(Video).where(Video.active.is_(True))):
        snapshots = list(session.scalars(select(Snapshot).where(Snapshot.video_id == v.id,
            Snapshot.observed_at >= now-timedelta(days=4)).order_by(Snapshot.observed_at)))
        # Consistent calendar window across videos; missing rows remain unknown.
        end = session.scalar(select(func.max(Daily.day)))
        daily = list(session.scalars(select(Daily).where(Daily.video_id == v.id,
            Daily.day >= end-timedelta(days=27), Daily.day <= end).order_by(Daily.day))) if end else []
        f = features(v, snapshots, daily)
        if snapshots and (aware(now)-aware(snapshots[-1].observed_at)).total_seconds() > settings.sync_interval_seconds*3:
            f.update(velocity=None, acceleration=None, quality="Snapshots veraltet")
        f["analytics_end"] = str(daily[-1].day) if daily else None
        traffic = session.scalar(select(Report).where(Report.video_id == v.id, Report.kind == "traffic")
                                 .order_by(Report.end.desc()).limit(1))
        f["organic_views_reported"] = sum(row["views"] for row in traffic.rows
            if row["insightTrafficSourceType"] != "ADVERTISING") if traffic and traffic.rows else None
        f["advertising_views_reported"] = sum(row["views"] for row in traffic.rows
            if row["insightTrafficSourceType"] == "ADVERTISING") if traffic and traffic.rows else None
        f["age_days"] = max(0, (aware(now)-aware(v.published_at)).days)
        result.append((v, snapshots, f))
    return result


def dashboard_rows(session, now=None):
    all_rows = latest_features(session, now)
    output = []
    for video, snapshots, f in all_rows:
        # Compare format and coarse age cohort; never infer Shorts from length.
        cohort = lambda p: (p["content_type"], min(p["age_days"]//30, 3))
        peers = [other for v, _, other in all_rows if v.id != video.id
                 and cohort(other) == cohort(f) and f["content_type"] != "UNKNOWN"]
        forecasts = []
        for h in (24, 168, 720):
            p = session.scalar(select(Forecast).where(Forecast.video_id == video.id, Forecast.horizon_hours == h)
                               .order_by(Forecast.origin_at.desc()).limit(1))
            if p:
                forecasts.append(dict(hours=h, views=p.predicted_views, lower=p.lower_views, upper=p.upper_views,
                    p100k=p.p100k, p1m=p.p1m, n=p.calibration_n, model=p.model_version,
                    origin=p.origin_at, target=p.target_at))
        explanation = momentum(f, peers)
        current_traffic = session.scalar(select(Report).where(Report.video_id == video.id, Report.kind == "traffic")
                                        .order_by(Report.end.desc()).limit(1))
        previous_traffic = session.scalar(select(Report).where(Report.video_id == video.id, Report.kind == "traffic_previous")
                                         .order_by(Report.end.desc()).limit(1))
        if current_traffic and previous_traffic and current_traffic.rows and previous_traffic.rows:
            ctotal = sum(r["views"] for r in current_traffic.rows)
            ptotal = sum(r["views"] for r in previous_traffic.rows)
            previous = {r["insightTrafficSourceType"]:r["views"] for r in previous_traffic.rows}
            if ctotal and ptotal:
                for row in current_traffic.rows:
                    source = row["insightTrafficSourceType"]
                    delta = row["views"]/ctotal-previous.get(source,0)/ptotal
                    if abs(delta) >= 0.1:
                        explanation["reasons"].append(f"Traffic-Anteil {source}: {delta*100:+.1f} Prozentpunkte gegenüber Vorperiode; mögliche Erklärung, kein Kausalnachweis.")
        if (f.get("advertising_views_reported") or 0) > 0:
            explanation.update(score=None, direction="Werbung enthalten")
            explanation["reasons"].append("Gesamtzähler enthalten Werbetraffic. Organisches Momentum und neue Gesamtzähler-Prognosen ausgesetzt.")
        output.append(dict(id=video.id, title=video.title, published_at=video.published_at,
            duration=video.duration_seconds, focus=video.id == settings.focus_video_id,
            last_snapshot=snapshots[-1].observed_at if snapshots else None,
            **f, **explanation, forecasts=forecasts,
            peer_count=len(peers)))
    return sorted(output, key=lambda r: r["score"] if r["score"] is not None else -1, reverse=True)


def ingest_reach(session, client, issues):
    ids = set(session.scalars(select(Video.id)))
    reports = list(client.reach_reports())
    if not reports:
        issues.append("reach: Reporting-Job erstellt; Google hat noch keine Berichte bereitgestellt.")
    for report in reports:
        # Re-import available reports to capture official corrections; overwrite, never add duplicates.
        grouped = defaultdict(lambda: [0, 0.0])
        for row in client.download_reach(report["downloadUrl"]):
            if row["video_id"] not in ids:
                continue
            day = date.fromisoformat(row["date"]) if "-" in row["date"] else datetime.strptime(row["date"], "%Y%m%d").date()
            count = int(row["video_thumbnail_impressions"])
            ctr = float(row["video_thumbnail_impressions_ctr"])
            if not 0 <= ctr <= 1 or count < 0:
                raise ValueError("Invalid reach metric")
            grouped[(row["video_id"], day)][0] += count
            grouped[(row["video_id"], day)][1] += count*ctr
        for (video_id, day), (count, weighted) in grouped.items():
            session.merge(Reach(video_id=video_id, day=day, impressions=count,
                ctr=weighted/count if count else None, report_id=report["id"]))


def collect(client=None):
    # Dedicated connection holds lock across per-video commits.
    with engine.connect() as lock:
        locked = engine.dialect.name == "postgresql"
        if locked and not lock.scalar(text("SELECT pg_try_advisory_lock(71402951)")):
            return {"status": "already_running"}
        try:
            return _collect(client or YouTube())
        finally:
            if locked:
                lock.execute(text("SELECT pg_advisory_unlock(71402951)"))


def _collect(client):
    issues, now = [], utcnow()
    with Session() as s:
        run = SyncRun()
        s.add(run)
        s.commit()
        run_id = run.id
    try:
        raw = client.channel()
        with Session() as s:
            existing_channel = s.scalar(select(Channel.id))
            if existing_channel and existing_channel != raw["id"]:
                raise ValueError("Separate databases required for demo or another channel.")
        with Session() as s:
            s.merge(Channel(id=raw["id"], title=raw["snippet"]["title"],
                subscribers=int(raw["statistics"]["subscriberCount"]) if "subscriberCount" in raw["statistics"] else None,
                updated_at=now))
            s.commit()
        playlist = raw["contentDetails"]["relatedPlaylists"]["uploads"]
        videos = list(client.videos(playlist))  # Do not deactivate anything on pagination failure.
        with Session() as s:
            existing = list(s.scalars(select(Video).where(Video.channel_id == raw["id"])))
            returned = {v["id"] for v in videos}
            for video in existing:
                video.active = video.id in returned
            for item in videos:
                snippet, stats = item["snippet"], item["statistics"]
                v = Video(id=item["id"], channel_id=raw["id"], title=snippet["title"], active=True,
                    published_at=datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00")),
                    duration_seconds=isodate.parse_duration(item["contentDetails"]["duration"]).total_seconds())
                s.merge(v)
                s.flush()
                latest = s.scalar(select(Snapshot).where(Snapshot.video_id == v.id).order_by(Snapshot.observed_at.desc()))
                if latest is None or (now-aware(latest.observed_at)).total_seconds() >= 300:
                    s.add(Snapshot(video_id=v.id, observed_at=now, views=int(stats["viewCount"]),
                        likes=int(stats["likeCount"]) if "likeCount" in stats else None,
                        comments=int(stats["commentCount"]) if "commentCount" in stats else None))
            s.commit()
        end = now.astimezone(ZoneInfo("America/Los_Angeles")).date()-timedelta(days=max(2, settings.analytics_lag_days))
        for item in sorted(videos, key=lambda v: v["id"] != settings.focus_video_id):
            with Session() as s:
                video = s.get(Video, item["id"])
                first = max(date(2009, 1, 1), aware(video.published_at).astimezone(ZoneInfo("America/Los_Angeles")).date())
                cursor = s.get(IngestCursor, (video.id, "daily"))
                last = cursor.through if cursor else s.scalar(select(func.max(Daily.day)).where(Daily.video_id == video.id))
                start = max(first, last-timedelta(days=30)) if last else first
                try:
                    while start <= end:
                        stop = min(end, start+timedelta(days=179))
                        rows = client.query(video.id, start, stop, METRICS, "day")
                        # Successful refresh replaces its window, including disappeared/suppressed rows.
                        s.execute(delete(Daily).where(Daily.video_id == video.id, Daily.day >= start, Daily.day <= stop))
                        for row in rows:
                            s.add(Daily(video_id=video.id, day=date.fromisoformat(row["day"]),
                                views=row["views"], watch_minutes=row["estimatedMinutesWatched"],
                                average_duration=row["averageViewDuration"], average_percentage=row["averageViewPercentage"],
                                subscribers_gained=row["subscribersGained"], subscribers_lost=row["subscribersLost"],
                                likes=row["likes"], comments=row["comments"], fetched_at=now))
                        s.merge(IngestCursor(video_id=video.id, kind="daily", through=stop, updated_at=now))
                        s.commit()
                        start = stop+timedelta(days=1)
                    recent = max(first, end-timedelta(days=27))
                    if recent > end:
                        continue
                    types = client.query(video.id, recent, end, "views", "creatorContentType")
                    if types:
                        kind = max(types, key=lambda r: r["views"])["creatorContentType"]
                        for d in s.scalars(select(Daily).where(Daily.video_id == video.id)):
                            d.content_type = kind
                    queries = [
                        ("traffic", recent, end, "views,estimatedMinutesWatched", "insightTrafficSourceType"),
                        ("retention", recent, end, "audienceWatchRatio,relativeRetentionPerformance", "elapsedVideoTimeRatio"),
                    ]
                    previous_end = recent-timedelta(days=1)
                    previous_start = max(first, previous_end-timedelta(days=27))
                    if previous_start <= previous_end:
                        queries.append(("traffic_previous", previous_start, previous_end,
                                        "views,estimatedMinutesWatched", "insightTrafficSourceType"))
                    if video.id == settings.focus_video_id:
                        queries.append(("retention_lifetime", first, end, "audienceWatchRatio,relativeRetentionPerformance", "elapsedVideoTimeRatio"))
                    if settings.enable_revenue:
                        queries.append(("revenue", recent, end, "estimatedRevenue,views", "day"))
                    for kind, begin, finish, metrics, dimensions in queries:
                        try:
                            rows = client.query(video.id, begin, finish, metrics, dimensions)
                            s.merge(Report(video_id=video.id, kind=kind, start=begin, end=finish, rows=rows, fetched_at=now))
                        except Exception as exc:
                            issues.append(f"{video.id}/{kind}: {type(exc).__name__}")
                    s.commit()
                except Exception as exc:
                    s.rollback()
                    issues.append(f"{video.id}/analytics: {type(exc).__name__}")
        if settings.enable_reach:
            with Session() as s:
                try:
                    ingest_reach(s, client, issues)
                    s.commit()
                except Exception as exc:
                    issues.append(f"reach: {type(exc).__name__}")
        with Session() as s:
            from .memory import evaluate_memory, strategy_feature
            mature(s, now)
            s.flush()
            evaluate_memory(s, now)
            s.flush()
            for video, snapshots, f in latest_features(s, now):
                if f["velocity"] is None or not snapshots or (f.get("advertising_views_reported") or 0) > 0:
                    continue
                f["strategy_evidence"] = strategy_feature(s, video.id, now)
                for h in (24, 168, 720):
                    origin = snapshots[-1]
                    exists = s.scalar(select(Forecast.id).where(Forecast.video_id == video.id,
                        Forecast.origin_at == origin.observed_at, Forecast.horizon_hours == h))
                    if not exists:
                        s.add(predict(s, video.id, origin, f, h))
            s.commit()
    except Exception as exc:
        issues.append(f"pipeline: {type(exc).__name__}")
        log.error("Collection failed (%s); secrets and raw API responses omitted", type(exc).__name__)
    with Session() as s:
        run = s.get(SyncRun, run_id)
        run.finished_at, run.issues = utcnow(), issues
        run.status = "failed" if any(i.startswith("pipeline:") for i in issues) else "partial" if issues else "ok"
        s.commit()
        return {"status": run.status, "issues": issues}
