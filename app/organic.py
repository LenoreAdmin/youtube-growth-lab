"""Conservative eligibility: no absent report is interpreted as organic evidence."""
from datetime import timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .models import Report, Daily, Snapshot
from .metrics import aware


def eligibility(session, forecast, cutoff):
    if (forecast.features.get("advertising_views_reported") or 0)>0:
        return {"status":"paid_excluded"}
    if forecast.actual_views is None:
        return {"status":"pending_outcome"}
    # All input quality windows plus the outcome must be covered, including boundary days.
    start=(aware(forecast.origin_at)-timedelta(days=32)).astimezone(ZoneInfo("America/Los_Angeles")).date()
    end=aware(forecast.actual_at).astimezone(ZoneInfo("America/Los_Angeles")).date()
    reports=list(session.scalars(select(Report).where(Report.video_id==forecast.video_id,
        Report.kind=="traffic",Report.start<=end,Report.end>=start,Report.fetched_at<=cutoff)))
    covered=set(); sources=[]
    for report in reports:
        if any(r.get("insightTrafficSourceType")=="ADVERTISING" and r.get("views",0)>0 for r in report.rows):
            return {"status":"paid_excluded"}
        daily=list(session.scalars(select(Daily).where(Daily.video_id==forecast.video_id,
            Daily.day>=report.start,Daily.day<=report.end,Daily.fetched_at<=cutoff)))
        expected=(report.end-report.start).days+1
        if not report.rows or len(daily)!=expected or sum(d.views for d in daily)!=sum(r.get("views",0) for r in report.rows):
            continue
        covered.update(d.day for d in daily)
        sources.append({"start":str(report.start),"end":str(report.end),"fetched_at":aware(report.fetched_at).isoformat()})
    if any(start+timedelta(days=i) not in covered for i in range((end-start).days+1)):
        return {"status":"insufficient_traffic_coverage"}
    snapshots=list(session.scalars(select(Snapshot).where(Snapshot.video_id==forecast.video_id,
        Snapshot.observed_at>=aware(forecast.origin_at)-timedelta(days=32),Snapshot.observed_at<=forecast.actual_at).order_by(Snapshot.observed_at)))
    if any(b.views<a.views for a,b in zip(snapshots,snapshots[1:])):
        return {"status":"counter_correction"}
    return {"status":"eligible_organic","reports":sources,"coverage_start":str(start),"coverage_end":str(end)}
