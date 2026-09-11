from datetime import date
from sqlalchemy import select
from app.pipeline import ingest_reach
from app.models import Reach
from app.youtube import YouTube

class FakeReach:
    def reach_reports(self):
        return [{"id":"r1","downloadUrl":"fixture"}]
    def download_reach(self, url):
        return [
            {"video_id":"a","date":"20260901","video_thumbnail_impressions":"100","video_thumbnail_impressions_ctr":"0.1"},
            {"video_id":"a","date":"20260901","video_thumbnail_impressions":"300","video_thumbnail_impressions_ctr":"0.2"}
        ]

def test_reach_weighting_and_idempotency(session):
    for _ in range(2):
        ingest_reach(session,FakeReach(),[])
        session.commit()
    rows=list(session.scalars(select(Reach)))
    assert len(rows)==1
    assert rows[0].impressions==400
    assert abs(rows[0].ctr-.175)<1e-9

def test_pagination_reads_all_rows_and_headers():
    class Request:
        def __init__(self, start): self.start=start
        def execute(self,num_retries):
            rows=[[str(i),i] for i in range(200)] if self.start==1 else [["last",9]]
            return {"columnHeaders":[{"name":"day"},{"name":"views"}],"rows":rows}
    class Reports:
        def reports(self): return self
        def query(self,**kwargs): return Request(kwargs["startIndex"])
    client=YouTube.__new__(YouTube)
    client.analytics=Reports()
    rows=client.query("a",date(2026,1,1),date(2026,9,1),"views","day")
    assert len(rows)==201
    assert rows[-1]["views"]==9

def test_download_rejects_non_google_host():
    import pytest
    client=YouTube.__new__(YouTube)
    with pytest.raises(ValueError):
        client.download_reach("https://attacker.example/file")


def test_advertising_disables_organic_momentum(session):
    from app.models import Report, Snapshot, utcnow
    from app.pipeline import dashboard_rows
    from datetime import timedelta
    now=utcnow()
    for days,views in ((2,100),(1,200),(0,400)):
        session.add(Snapshot(video_id="a",observed_at=now-timedelta(days=days),views=views))
    session.add(Report(video_id="a",kind="traffic",start=(now-timedelta(days=28)).date(),end=now.date(),
        rows=[{"insightTrafficSourceType":"ADVERTISING","views":100},
              {"insightTrafficSourceType":"RELATED_VIDEO","views":300}]))
    session.flush()
    video=next(r for r in dashboard_rows(session,now) if r["id"]=="a")
    assert video["score"] is None
    assert video["organic_views_reported"] == 300
    assert video["advertising_views_reported"] == 100
