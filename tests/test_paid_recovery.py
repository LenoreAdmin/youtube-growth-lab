"""V5.1: historical ads never block today's organic evaluation; current ads do; cooldown in between; organic peaks only."""
from datetime import date, timedelta
from sqlalchemy import select
from app import history, regimes, learning, growth_engine as ge
from app.models import GrowthScore, GrowthPlan, Daily
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG

KNOWN_END = TODAY-timedelta(days=LAG)


def load(session, video_id="a"):
    return next(h for h in history.load(session) if h.video.id == video_id)


def test_old_paid_days_leave_today_organic_but_stay_auditable(monkeypatch, session):
    wire(monkeypatch, session)
    paid = {TODAY-timedelta(days=d) for d in range(200, 226)}  # 26 ad days, long ago
    seed_history(session, "a", days=800, paid_days=paid)
    seed_history(session, "b", days=800, base=200, seed=4)
    h = load(session)
    profile = history.paid_profile(h, TODAY)
    assert profile["status"] == "organic_with_paid_history" and profile["paid_days_total"] == 26
    assert profile["last_paid_day"] == str(TODAY-timedelta(days=200)) and profile["paid_views_32d"] == 0 and profile["days_until_clean"] == 0
    f = history.features_at(h, TODAY)
    assert f["paid"]["status"] == "organic_with_paid_history" and f["paid_views_32d"] == 0
    learning.refresh(session, NOW)
    session.expire_all()
    score = session.scalar(select(GrowthScore).where(GrowthScore.video_id == "a"))
    assert score.state not in ("paid_excluded", "paid_cooldown") and score.opportunity["score"] is not None
    assert score.viewer["score"] is not None and score.subscriber["score"] is not None
    assert score.momentum["paid"]["paid_days_total"] == 26
    row = next(r for r in session.scalar(select(GrowthPlan)).plan["ranking"] if r["video_id"] == "a")
    assert row["paid_status"] == "organic_with_paid_history" and "26 Werbetage" in row["paid_note"]
    # Training still excludes every paid-affected origin.
    rows, excluded = history.build(history.load(session), 168, LAG)
    assert excluded["paid_feature_window"] > 0 and all(r["features"]["paid_views_32d"] == 0 for r in rows)


def test_current_ads_are_paid_excluded_including_lag_gap(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, paid_days={KNOWN_END-timedelta(days=2)})
    h = load(session)
    profile = history.paid_profile(h, TODAY)
    assert profile["status"] == "paid_excluded" and profile["paid_views_7d"] > 0
    f = history.features_at(h, TODAY)
    base = regimes.baselines(history.build(history.load(session), 168, LAG)[0])
    regime = regimes.classify(f, base)
    assert regime["regime"] == "paid_excluded" and "7 bekannten Tagen" in regime["reason"]
    assert ge.state_of(f, regime, base, {"candidate": False}) == "paid_excluded"
    board = ge.scores(f, regime, base, [], None, None)
    assert board["opportunity"]["score"] is None and "Werbetraffic" in board["opportunity"]["reason"]
    assert ge.revival(f, regime, base, None)["candidate"] is False
    # Ads inside the analytics lag gap are known to the owner and count as current contamination.
    gap = TODAY-timedelta(days=1)
    seed_history(session, "b", days=400, seed=2)
    from app.models import TrafficDaily
    session.add(TrafficDaily(video_id="b", day=gap, source="ADVERTISING", views=3, watch_minutes=0, paid=True, fetched_at=NOW))
    session.commit()
    hb = load(session, "b")
    assert history.paid_profile(hb, TODAY)["paid_views_lag_gap"] == 3
    assert history.paid_profile(hb, TODAY)["status"] == "paid_excluded"
    assert regimes.classify(history.features_at(hb, TODAY), base)["regime"] == "paid_excluded"


def test_recent_ads_enter_cooldown_with_visible_countdown(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, paid_days={KNOWN_END-timedelta(days=15)})
    seed_history(session, "b", days=400, base=200, seed=5)
    h = load(session)
    profile = history.paid_profile(h, TODAY)
    assert profile["status"] == "paid_cooldown" and profile["paid_views_7d"] == 0 and profile["paid_views_32d"] > 0
    assert profile["clean_days"] == 15 and profile["days_until_clean"] == 17 and profile["required_clean_days"] == 32
    learning.refresh(session, NOW)
    session.expire_all()
    score = session.scalar(select(GrowthScore).where(GrowthScore.video_id == "a"))
    assert score.state == "paid_cooldown" and score.action == "observe" and score.opportunity["score"] is None
    assert "17 weiteren sauberen Tagen" in score.opportunity["reason"]
    row = next(r for r in session.scalar(select(GrowthPlan)).plan["ranking"] if r["video_id"] == "a")
    assert row["paid_status"] == "paid_cooldown" and row["paid"]["days_until_clean"] == 17 and any("Cooldown" in n for n in row["notes"])
    assert row["priority"] == 2  # never ranked above an organically scored video
    # Once the clean window reaches 32 known days the same video is scored again automatically.
    later = NOW+timedelta(days=17)
    for video_id in ("a", "b"):
        for i in range(1, 18):
            session.add(Daily(video_id=video_id, day=KNOWN_END+timedelta(days=i), views=100, watch_minutes=250, average_duration=150,
                              average_percentage=45, subscribers_gained=2, subscribers_lost=0, likes=5, comments=1, content_type="VIDEO", fetched_at=later))
    session.commit()
    profile = history.paid_profile(load(session), later.date())
    assert profile["status"] == "organic_with_paid_history" and profile["clean_days"] == 32
    monkeypatch.setattr(learning, "REBUILD_HOURS", 10**6)
    learning.refresh(session, later)
    session.expire_all()
    score = session.scalar(select(GrowthScore).where(GrowthScore.video_id == "a").order_by(GrowthScore.day.desc()))
    assert score.state not in ("paid_cooldown", "paid_excluded") and score.opportunity["score"] is not None


def test_organic_revival_after_paid_uses_organic_peak_only(monkeypatch, session):
    wire(monkeypatch, session)
    # Paid campaign 120 days ago with a huge inflated week; organic history is flat and modest.
    paid_week = {TODAY-timedelta(days=d) for d in range(120, 127)}
    seed_history(session, "a", days=900, base=100, trend=0, paid_days=paid_week)
    for d in paid_week:
        session.get(Daily, ("a", d)).views = 5000
    session.commit()
    h = load(session)
    peak = ge.historical_peak(h, TODAY)
    assert peak["basis"] == "organic_weeks_only" and peak["peak_velocity"] < 200
    assert peak["paid_peak_velocity"] > 3000 and peak["paid_days_total"] == 7
    # Weeks inside the 32-day spillover after the campaign are excluded from the organic peak too.
    tainted_end = TODAY-timedelta(days=100)
    for i in range(7):
        session.get(Daily, ("a", tainted_end-timedelta(days=i))).views = 2500
    session.commit()
    assert ge.historical_peak(load(session), TODAY)["peak_velocity"] < 200
    profile = history.paid_profile(load(session), TODAY)
    assert profile["status"] == "organic_with_paid_history"
    # Revival can now trigger organically: retention above median and pace far below the (organic) peak plus conversion.
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    f = history.features_at(load(session), TODAY)
    lifted = {**f, "retention_avg": base["medians"]["retention_avg"]["median"]*1.5, "subscriber_conversion_7d": base["medians"]["subscriber_conversion_7d"]["median"]*2,
              "velocity_7d": 10, "ratio_7_28": .9}
    regime = regimes.classify(lifted, base)
    assert regime["regime"] != "paid_excluded"
    rev = ge.revival(lifted, regime, base, ge.historical_peak(load(session), TODAY))
    assert rev["candidate"] and rev["post_paid"] and "organisches Revival nach Werbung" in rev["reason"]
    assert rev["far_below_peak"] is True  # judged against the organic peak (<200), not the paid peak (>3000)


def test_breakout_never_rests_on_paid_views(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, paid_days={KNOWN_END-timedelta(days=1), KNOWN_END})
    for i in range(3):
        session.get(Daily, ("a", KNOWN_END-timedelta(days=i))).views = 50000
    session.commit()
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    f = history.features_at(load(session), TODAY)
    regime = regimes.classify(f, base)
    assert regime["regime"] == "paid_excluded" and ge.state_of(f, regime, base, {"candidate": False}) == "paid_excluded"
    assert ge.scores(f, regime, base, [], None, None)["viewer"]["score"] is None
