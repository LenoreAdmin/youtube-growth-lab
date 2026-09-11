"""Explicit synthetic fixture only. Refuses to mix with an existing channel."""
from datetime import timedelta
from sqlalchemy import select
from .db import Session
from .models import Channel, Video, Snapshot, Daily, Report, SyncRun, utcnow
from .pipeline import latest_features
from .prediction import predict


def seed():
    now = utcnow().replace(microsecond=0)
    with Session() as s:
        if s.scalar(select(Channel.id)):
            raise ValueError("Demo requires an empty database.")
        s.add(Channel(id="DEMO_CHANNEL", title="Demo · keine echten Kanaldaten", subscribers=600))
        s.flush()
        examples = [("demo_momentum", "Warum dieser Einstieg Zuschauer hält", 20340, 75, 130),
                    ("demo_stable", "Ein Thema, drei Perspektiven", 8340, 24, 25),
                    ("demo_slow", "Was ich beim nächsten Video ändere", 3150, 22, 9)]
        for i, (video_id, title, total, before, after) in enumerate(examples):
            s.add(Video(id=video_id, channel_id="DEMO_CHANNEL", title=title,
                published_at=now-timedelta(days=45+i), duration_seconds=480))
            s.flush()
            for h in range(73):
                hours_back = 72-h
                views = total-round(min(hours_back, 24)*after+max(0, hours_back-24)*before)
                s.add(Snapshot(video_id=video_id, observed_at=now-timedelta(hours=hours_back),
                    views=max(0, views), likes=max(0, views//30), comments=max(0, views//150)))
            for d in range(31):
                day = (now-timedelta(days=3+d)).date()
                views = round(after*12)
                s.add(Daily(video_id=video_id, day=day, views=views, watch_minutes=views*(4-i),
                    average_duration=240-i*60, average_percentage=50-i*12.5, subscribers_gained=views//80,
                    subscribers_lost=1, likes=views//30, comments=views//150, content_type="VIDEO"))
            s.add(Report(video_id=video_id, kind="retention", start=(now-timedelta(days=30)).date(),
                end=(now-timedelta(days=3)).date(), rows=[
                    {"elapsedVideoTimeRatio": x/10, "audienceWatchRatio": 0.9-x*0.055,
                     "relativeRetentionPerformance": 0.55} for x in range(1, 11)]))
        s.add(SyncRun(status="demo", finished_at=now, issues=["Synthetische Beispieldaten"]))
        s.commit()
        for video, snapshots, f in latest_features(s, now):
            if f["velocity"] is not None:
                for h in (24, 168, 720):
                    s.add(predict(s, video.id, snapshots[-1], f, h))
        s.commit()
    print("Synthetic demo created. Do not use this database with real OAuth credentials.")
