from datetime import timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
from app.models import Snapshot, Daily, IngestCursor, utcnow
from app import pipeline

class FakeYouTube:
    def __init__(self, fail=False):
        self.fail=fail
    def channel(self):
        return {"id":"channel","snippet":{"title":"Real channel fixture"},"statistics":{"subscriberCount":"600"},
                "contentDetails":{"relatedPlaylists":{"uploads":"uploads"}}}
    def videos(self, playlist):
        yield {"id":"a","snippet":{"title":"Fixture","publishedAt":(utcnow()-timedelta(days=45)).isoformat()},
            "contentDetails":{"duration":"PT10M"},"statistics":{"viewCount":"20000","likeCount":"500","commentCount":"25"}}
    def query(self, video, start, end, metrics, dimensions):
        if self.fail:
            raise RuntimeError("Simulated API failure")
        if dimensions=="day":
            return [{"day":str(end),"views":100,"estimatedMinutesWatched":500,
                    "averageViewDuration":300,"averageViewPercentage":50,"subscribersGained":2,
                    "subscribersLost":0,"likes":3,"comments":1}]
        if dimensions=="creatorContentType":
            return [{"creatorContentType":"VIDEO","views":100}]
        return []
    def reach_reports(self):
        return []

def wire(monkeypatch, session):
    monkeypatch.setattr(pipeline,"Session",sessionmaker(session.bind,expire_on_commit=False))
    monkeypatch.setattr(pipeline,"engine",session.bind)
    monkeypatch.setattr(pipeline.settings,"enable_reach",False)

def test_complete_import_is_idempotent(monkeypatch,session):
    wire(monkeypatch,session)
    assert pipeline.collect(FakeYouTube())["status"]=="ok"
    assert pipeline.collect(FakeYouTube())["status"]=="ok"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Snapshot))==1
    assert session.scalar(select(func.count()).select_from(Daily))==1
    assert session.get(IngestCursor,("a","daily")) is not None

def test_failed_refresh_preserves_existing_data(monkeypatch,session):
    wire(monkeypatch,session)
    pipeline.collect(FakeYouTube())
    count=session.scalar(select(func.count()).select_from(Daily))
    result=pipeline.collect(FakeYouTube(fail=True))
    session.expire_all()
    assert result["status"]=="partial"
    assert session.scalar(select(func.count()).select_from(Daily))==count

def test_mixing_channels_is_rejected(monkeypatch,session):
    wire(monkeypatch,session)
    class WrongChannel(FakeYouTube):
        def channel(self):
            result=super().channel()
            result["id"]="another-channel"
            return result
    assert pipeline.collect(WrongChannel())["status"]=="failed"
