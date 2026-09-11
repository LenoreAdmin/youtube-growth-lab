from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
from sqlalchemy import select, func, text, delete
import isodate
from .config import settings
from .db import Session, engine
from .models import Channel, Video, Snapshot, Daily, Report, Reach, SyncRun, Forecast, IngestCursor, ImportedReport, GrowthAssessment, utcnow
from .metrics import features, aware
from .growth import enrich, assess, VERSION
from .prediction import mature, predict
from .youtube import YouTube
from .budget import Budget, SyncBudgetExceeded
from .jobs import acquire, release

log = logging.getLogger(__name__)
METRICS = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost,likes,comments"


def latest_features(session, now=None, budget=None):
    now = now or utcnow()
    result = []
    for v in session.scalars(select(Video).where(Video.active.is_(True))):
        if budget:
            budget.check()
        snapshots = list(session.scalars(select(Snapshot).where(Snapshot.video_id == v.id,
            Snapshot.observed_at >= now-timedelta(days=62), Snapshot.observed_at <= now).order_by(Snapshot.observed_at)))
        # Consistent calendar window across videos; missing rows remain unknown.
        end = session.scalar(select(func.max(Daily.day)).where(Daily.day <= now.date(), Daily.fetched_at <= now))
        daily = list(session.scalars(select(Daily).where(Daily.video_id == v.id,
            Daily.day >= end-timedelta(days=27), Daily.day <= end, Daily.fetched_at <= now).order_by(Daily.day))) if end else []
        f = enrich(features(v, snapshots, daily), snapshots, now)
        reaches = list(session.scalars(select(Reach).where(Reach.video_id == v.id,
            Reach.day >= now.date()-timedelta(days=30), Reach.day <= now.date(), Reach.ctr.is_not(None))))
        impressions = sum(r.impressions for r in reaches)
        f["ctr"] = sum(r.impressions*r.ctr for r in reaches)/impressions if impressions else None
        f["reach_end"] = str(max(r.day for r in reaches)) if reaches else None
        f["metric_status"] = {"analytics": "available", "ctr": "available" if reaches else "missing"}
        if not daily or (now.date()-daily[-1].day).days > max(7, settings.analytics_lag_days+3):
            f.update(subscriber_conversion=None, watchtime_efficiency=None)
            f["metric_status"]["analytics"] = "delayed_or_missing"
        if reaches and (now.date()-max(r.day for r in reaches)).days > 7:
            f["ctr"] = None
            f["metric_status"]["ctr"] = "delayed"
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
        explanation = assess(f, peers)
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
                        explanation["reasons"].append(f"Traffic-Anteil {source}: {delta*100:+.1f} Prozentpunkte gegenÃ¼ber Vorperiode; mÃ¶gliche ErklÃ¤rung, kein Kausalnachweis.")
        if (f.get("advertising_views_reported") or 0) > 0:
            explanation.update(score=None, direction="Werbung enthalten")
            explanation["reasons"].append("GesamtzÃ¤hler enthalten Werbetraffic. Organisches Momentum und neue GesamtzÃ¤hler-Prognosen ausgesetzt.")
        output.append(dict(id=video.id, title=video.title, published_at=video.published_at,
            duration=video.duration_seconds, focus=video.id == settings.focus_video_id,
            last_snapshot=snapshots[-1].observed_at if snapshots else None,
            **f, **explanation, forecasts=forecasts,
            peer_count=len(peers)))
    return sorted(output, key=lambda r: r["score"] if r["score"] is not None else -1, reverse=True)


def ingest_reach(session, client, issues, budget=None):
    ids = set(session.scalars(select(Video.id)))
    reports = list(client.reach_reports())
    if not reports:
        issues.append("reach: Reporting-Job erstellt; Google hat noch keine Berichte bereitgestellt.")
    for report in reports:
        if budget:
            budget.check()
        if session.get(ImportedReport, report["id"]):
            continue
        # Replacement reports have new IDs; imported IDs need no further downloads.
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
        session.add(ImportedReport(id=report["id"]))
        session.commit()


def collect(client=None, bucket=None):
    owner, skipped = acquire(Session, bucket)
    if skipped:
        return {"status": skipped}
    result = None
    try:
        budget = Budget(settings.sync_budget_seconds)
        result = _collect(client, budget)
        return result
    finally:
        completed = bucket if result and result["status"] in ("ok", "deferred") else None
        release(Session, owner, completed)


def _collect(client, budget=None):
    budget = budget or Budget(settings.sync_budget_seconds)
    issues, now, deferred = [], utcnow(), False
    with Session() as s:
        run = SyncRun()
        s.add(run)
        s.commit()
        run_id = run.id
    try:
        client = client or YouTube(budget=budget)
        budget.check()
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
        with Session() as s:
            attempts = {c.video_id: aware(c.updated_at).timestamp() for c in s.scalars(
                select(IngestCursor).where(IngestCursor.kind == "attempt"))}
        ordered = sorted(videos, key=lambda v: (attempts.get(v["id"], 0), v["id"] != settings.focus_video_id))
        for item in ordered:
            budget.check()
            with Session() as s:
                video = s.get(Video, item["id"])
                s.merge(IngestCursor(video_id=video.id, kind="attempt", through=end, updated_at=now))
                s.commit()
                first = max(date(2009, 1, 1), aware(video.published_at).astimezone(ZoneInfo("America/Los_Angeles")).date())
                cursor = s.get(IngestCursor, (video.id, "daily"))
                last = cursor.through if cursor else s.scalar(select(func.max(Daily.day)).where(Daily.video_id == video.id))
                start = max(first, last-timedelta(days=30)) if last else first
                try:
                    while start <= end:
                        budget.check()
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
                    if settings.enable_revenue:
                        queries.append(("revenue", recent, end, "estimatedRevenue,views", "day"))
                    for kind, begin, finish, metrics, dimensions in queries:
                        try:
                            rows = client.query(video.id, begin, finish, metrics, dimensions)
                            s.merge(Report(video_id=video.id, kind=kind, start=begin, end=finish, rows=rows, fetched_at=now))
                        except SyncBudgetExceeded:
                            raise
                        except Exception as exc:
                            issues.append(f"{video.id}/{kind}: {type(exc).__name__}")
                    s.commit()
                except SyncBudgetExceeded:
                    raise
                except Exception as exc:
                    s.rollback()
                    issues.append(f"{video.id}/analytics: {type(exc).__name__}")
        if settings.enable_reach:
            with Session() as s:
                try:
                    ingest_reach(s, client, issues, budget)
                    s.commit()
                except SyncBudgetExceeded:
                    raise
                except Exception as exc:
                    issues.append(f"reach: {type(exc).__name__}")
        with Session() as s:
            from .memory import evaluate_memory, strategy_feature
            mature(s, now)
            s.flush()
            evaluate_memory(s, now)
            s.flush()
            s.commit()
            all_features = latest_features(s, now, budget)
            for video, snapshots, f in all_features:
                budget.check()
                if snapshots:
                    peers = [other for peer, _, other in all_features if peer.id != video.id
                        and other["content_type"] == f["content_type"] != "UNKNOWN"
                        and min(other["age_days"]//30,3) == min(f["age_days"]//30,3)]
                    assessment = assess(f, peers)
                    f = {**f, "growth_assessment": assessment}
                    exists = s.scalar(select(GrowthAssessment.id).where(GrowthAssessment.video_id == video.id,
                        GrowthAssessment.origin_at == snapshots[-1].observed_at, GrowthAssessment.version == VERSION))
                    if not exists:
                        s.add(GrowthAssessment(video_id=video.id, origin_at=snapshots[-1].observed_at,
                            version=VERSION, features=f, assessment=assessment))
                        s.commit()
                if f["velocity"] is None or not snapshots or (f.get("advertising_views_reported") or 0) > 0:
                    continue
                f["strategy_evidence"] = strategy_feature(s, video.id, now)
                for h in (24, 168, 720):
                    origin = snapshots[-1]
                    exists = s.scalar(select(Forecast.id).where(Forecast.video_id == video.id,
                        Forecast.origin_at == origin.observed_at, Forecast.horizon_hours == h))
                    if not exists:
                        budget.check()
                        s.add(predict(s, video.id, origin, f, h))
                s.commit()
            s.commit()
        # Optional lifetime retention is last: all core results are already committed.
        optional_lifetime(client, now, end, budget, issues)
    except SyncBudgetExceeded:
        deferred = True
        issues.append("Zeitbudget erreicht; gespeicherter Fortschritt wird beim nÃ¤chsten Cron fortgesetzt.")
    except Exception as exc:
        issues.append(f"pipeline: {type(exc).__name__}")
        log.error("Collection failed (%s); secrets and raw API responses omitted", type(exc).__name__)
    with Session() as s:
        run = s.get(SyncRun, run_id)
        run.finished_at, run.issues = utcnow(), issues
        run.status = "deferred" if deferred else "failed" if any(i.startswith("pipeline:") for i in issues) else "partial" if any(not i.startswith("optional/") for i in issues) else "ok"
        s.commit()
        return {"status": run.status, "issues": issues}


def optional_lifetime(client, now, end, budget, issues):
    """One bounded request, weekly cooldown persisted before I/O; never retry in this run."""
    if not settings.focus_video_id:
        return
    try:
        budget.check()
        with Session() as s:
            video = s.get(Video, settings.focus_video_id)
            if not video or not video.active:
                return
            cursor = s.get(IngestCursor, (video.id, "retention_attempt"))
            if cursor and aware(cursor.updated_at) > now-timedelta(days=7):
                return
            first = aware(video.published_at).astimezone(ZoneInfo("America/Los_Angeles")).date()
            if first > end:
                return
            s.merge(IngestCursor(video_id=video.id, kind="retention_attempt", through=end, updated_at=now))
            s.commit()
            if isinstance(client, YouTube):
                rows = client.query(video.id, first, end, "audienceWatchRatio,relativeRetentionPerformance",
                                    "elapsedVideoTimeRatio", timeout=5, max_pages=1)
            else:
                rows = client.query(video.id, first, end, "audienceWatchRatio,relativeRetentionPerformance", "elapsedVideoTimeRatio")
            s.merge(Report(video_id=video.id, kind="retention_lifetime", start=first, end=end, rows=rows, fetched_at=now))
            s.commit()
    except SyncBudgetExceeded:
        # Skipped optional work must not downgrade a completed core sync.
        return
    except Exception as exc:
        issues.append(f"optional/retention_lifetime: {type(exc).__name__}; retry after cooldown")
