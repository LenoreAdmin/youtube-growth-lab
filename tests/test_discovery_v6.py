"""V6 discovery: real own-analytics evidence, budgeted public probes, caching, failures, opportunities, memory, V5 integration."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
from googleapiclient.errors import HttpError
from app import discovery, learning, growth_engine as ge, history, pipeline
from app.budget import SyncBudgetExceeded
from app.models import (DiscoveryRun, DiscoveryQuota, DiscoveryQuery, DiscoveryItem, DiscoveryChannel, DiscoverySignal, DiscoveryOpportunity,
                        GrowthPlan, Video, TrafficDaily)
from test_learning_v4 import seed_history, wire as wire_v4, NOW, TODAY, LAG


def item(video_id, title, channel="ch1", views=50000, days_old=120, tags=("train", "journey", "ambient")):
    published = (NOW-timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": video_id, "snippet": {"title": title, "channelId": channel, "channelTitle": "Channel "+channel, "publishedAt": published, "tags": list(tags)},
            "contentDetails": {"duration": "PT4M"}, "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"}}


class DiscoveryClient:
    """Deterministic public + analytics double. Counts calls so quota accounting can be verified."""
    def __init__(self, fail_query=None, throttle=False):
        self.calls = {"traffic_detail": 0, "search": 0, "videos_by_id": 0, "channels_by_id": 0}
        self.fail_query, self.throttle = fail_query, throttle
        self.catalog = {
            "ext00000001": item("ext00000001", "Night train journey ambient music", views=250000, days_old=200),
            "ext00000002": item("ext00000002", "Lofi train ride study session", channel="ch2", views=40000, days_old=60),
            "ext00000003": item("ext00000003", "Shine on cover acoustic", channel="ch3", views=8000, days_old=800, tags=("shine", "acoustic")),
            "ext00000004": item("ext00000004", "Railway cab ride 4k", channel="ch4", views=900000, days_old=400, tags=("railway", "cab")),
            "a": item("a", "Sealand – Trainstories (Official Video)", channel="own", views=20000, days_old=800, tags=("trainstories", "train", "journey", "instrumental")),
            "b": item("b", "Sealand – Shine On", channel="own", views=9000, days_old=700, tags=("shine on", "shine", "acoustic")),
        }

    def traffic_detail(self, video, start, end, source, max_results=25):
        self.calls["traffic_detail"] += 1
        if source == "YT_SEARCH":
            return {"a": [{"insightTrafficSourceDetail": "train journey music", "views": 40, "estimatedMinutesWatched": 80},
                          {"insightTrafficSourceDetail": "sealand trainstories", "views": 30, "estimatedMinutesWatched": 70}],
                    "b": [{"insightTrafficSourceDetail": "shine on acoustic", "views": 12, "estimatedMinutesWatched": 20}]}.get(video, [])
        if source == "RELATED_VIDEO":
            return {"a": [{"insightTrafficSourceDetail": "ext00000001", "views": 25, "estimatedMinutesWatched": 50}]}.get(video, [])
        return [{"insightTrafficSourceDetail": "https://example.org/blog", "views": 3, "estimatedMinutesWatched": 4}] if video == "a" else []

    def search(self, query, max_results=25, published_after=None, order="relevance"):
        self.calls["search"] += 1
        if self.throttle:
            raise HttpError(Mock(status=403, reason="quota"), b'{"error":{"message":"quotaExceeded"}}')
        if self.fail_query and query == self.fail_query:
            raise TimeoutError()
        ids = ["ext00000001", "ext00000002", "ext00000004"] if "train" in query or "journey" in query else ["ext00000003", "ext00000002"]
        if "trainstories" in query:
            ids = ["ext00000001", "a", "ext00000002"]
        return [{"video_id": i, "channel_id": self.catalog[i]["snippet"]["channelId"], "title": self.catalog[i]["snippet"]["title"],
                 "channel_title": "x", "published_at": self.catalog[i]["snippet"]["publishedAt"]} for i in ids]

    def videos_by_id(self, ids):
        self.calls["videos_by_id"] += 1
        return [self.catalog[i] for i in ids if i in self.catalog]

    def channels_by_id(self, ids):
        self.calls["channels_by_id"] += 1
        return [{"id": c, "snippet": {"title": "Channel "+c}, "statistics": {"subscriberCount": {"ch1": "50000", "ch2": "3000", "ch3": "400", "ch4": "2000000", "own": "600"}.get(c, "100"),
                 "videoCount": "40", "viewCount": "1000000"}} for c in ids]


def wire(monkeypatch, session, published=None):
    factory = wire_v4(monkeypatch, session)
    monkeypatch.setattr(discovery, "Session", factory)
    for v in session.scalars(select(Video)):
        v.title = {"a": "Sealand – Trainstories (Official Video)", "b": "Sealand – Shine On"}[v.id]
    session.commit()
    return factory


def test_run_collects_signals_probes_within_quota_and_is_idempotent(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=60, seed=2)
    client = DiscoveryClient()
    result = discovery.run(client, NOW)
    assert result["status"] == "ok" and result["stats"]["signals"] >= 5 and result["stats"]["searches"] > 0
    session.expire_all()
    units = result["stats"]["units_used"]
    assert units == client.calls["search"]*discovery.SEARCH_COST+(client.calls["videos_by_id"]+client.calls["channels_by_id"])*discovery.LIST_COST
    assert session.get(DiscoveryQuota, TODAY).units == units and units <= discovery.DAILY_UNITS
    terms = {r.detail: r.views for r in session.scalars(select(DiscoverySignal).where(DiscoverySignal.video_id == "a", DiscoverySignal.kind == "own_search_term"))}
    assert terms == {"train journey music": 40, "sealand trainstories": 30}
    assert session.get(DiscoveryItem, "ext00000001").via["suggested_source"] == ["own_traffic"]
    probed = [q for q in session.scalars(select(DiscoveryQuery)) if q.last_probed_day]
    assert probed and all(q.next_probe_day == TODAY+timedelta(days=discovery.REPROBE_DAYS) for q in probed)
    trainstories = session.scalar(select(DiscoveryQuery).where(DiscoveryQuery.query == "sealand trainstories"))
    assert trainstories.source == "own_search_term" and trainstories.results["our_rank"] == 2
    assert session.get(DiscoveryChannel, "ch4").subscribers == 2000000
    # Same day again: already completed; forced rerun re-uses cached probes and duplicates nothing.
    assert discovery.run(client, NOW+timedelta(hours=1))["status"] == "already_completed"
    unprobed = session.scalar(select(func.count()).select_from(DiscoveryQuery).where(DiscoveryQuery.last_probed_day.is_(None)))
    before = (client.calls["search"], session.scalar(select(func.count()).select_from(DiscoverySignal)), session.scalar(select(func.count()).select_from(DiscoveryOpportunity)))
    forced = discovery.run(client, NOW+timedelta(hours=2), force=True)
    session.expire_all()
    # Cached probes are not repeated; only queries that never ran are probed now.
    assert forced["status"] == "ok" and client.calls["search"] == before[0]+unprobed
    assert all(q.probe_count == 1 for q in session.scalars(select(DiscoveryQuery)) if q.last_probed_day)
    assert session.scalar(select(func.count()).select_from(DiscoverySignal)) == before[1]
    assert session.scalar(select(func.count()).select_from(DiscoveryOpportunity)) == before[2]
    assert session.scalar(select(func.count()).select_from(DiscoveryRun)) == 2


def test_daily_budget_stops_probing_and_resumes_next_day(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=60, seed=2)
    monkeypatch.setattr(discovery, "DAILY_UNITS", 250)
    client = DiscoveryClient()
    result = discovery.run(client, NOW)
    assert result["status"] == "quota_exhausted" and client.calls["search"] == 2
    session.expire_all()
    assert session.get(DiscoveryQuota, TODAY).units <= 250
    pending = [q for q in session.scalars(select(DiscoveryQuery)) if not q.last_probed_day]
    assert pending, "unprobed queries wait for the next day"
    tomorrow = NOW+timedelta(days=1)
    again = discovery.run(client, tomorrow)
    session.expire_all()
    assert again["status"] in ("ok", "quota_exhausted") and client.calls["search"] > 2
    assert session.get(DiscoveryQuota, TODAY+timedelta(days=1)).units <= 250
    assert session.scalar(select(func.count()).select_from(DiscoveryOpportunity).where(DiscoveryOpportunity.day == TODAY+timedelta(days=1))) > 0


def test_quota_errors_throttle_and_single_failures_back_off(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=60, seed=2)
    throttled = discovery.run(DiscoveryClient(throttle=True), NOW)
    assert throttled["status"] == "throttled"
    session.expire_all()
    assert all(q.failures == 0 for q in session.scalars(select(DiscoveryQuery)))
    assert session.scalar(select(func.count()).select_from(DiscoverySignal)) > 0  # analytics part already stored
    client = DiscoveryClient(fail_query="sealand trainstories")
    result = discovery.run(client, NOW+timedelta(days=1), force=True)
    session.expire_all()
    assert result["status"] == "ok" and result["stats"]["failures"] == 1
    failed = session.scalar(select(DiscoveryQuery).where(DiscoveryQuery.query == "sealand trainstories"))
    assert failed.failures == 1 and failed.next_probe_day == TODAY+timedelta(days=1+discovery.FAIL_BACKOFF_DAYS) and not failed.last_probed_day
    run = session.scalar(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()))
    assert run.status == "ok" and run.units_used > 0


def test_opportunities_are_classified_with_evidence_and_paid_is_never_demand(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=60, seed=2, paid_days={TODAY-timedelta(days=LAG+10)})  # b in paid cooldown
    discovery.run(DiscoveryClient(), NOW)
    session.expire_all()
    rows = {(r.kind, r.key): r for r in session.scalars(select(DiscoveryOpportunity).where(DiscoveryOpportunity.day == TODAY))}
    # Generische Ein-Wort-Seeds aus Titel/Tags werden nicht mehr geprobt.
    assert ("search", "trainstories") not in rows and ("search", "journey") not in rows
    branded = rows[("search", "sealand trainstories")]  # echter eigener Suchbegriff mit 30 Views
    assert branded.video_id == "a" and branded.evidence["evidence_level"] == "own_analytics"
    assert branded.evidence["actionable"] is True and branded.evidence["score_capped"] is False
    assert branded.evidence["probe"]["n"] == 3 and branded.evidence["probe"]["our_rank"] == 2
    real = rows[("search", "train journey")]  # "train journey music" normalises to this key: real own search views
    assert real.video_id == "a" and real.evidence["demand_source"] == "own_analytics" and real.evidence["own_search_views_90d"] == 40
    assert real.gap == "existing_video_opportunity"  # relevant, but Trainstories is not in the top results
    proxies = [c for c in real.components["components"] if c["evidence"] == "proxy"]
    assert proxies and all(c["available"] is not None for c in proxies)
    suggested = rows[("suggested", "ext00000001")]
    assert suggested.video_id == "a" and suggested.gap == "suggested_opportunity" and suggested.evidence["own_suggested_views_90d"] == 25
    assert suggested.scores["suggested_opportunity_score"] is not None and "nichts kopieren" in suggested.evidence["note"]
    clusters = [r for (k, _), r in rows.items() if k == "cluster"]
    assert clusters and all(r.evidence["n_members"] >= 2 for r in clusters)
    # Shine On is in paid cooldown: its own search views exist but are not used as organic demand evidence.
    shine = rows[("search", "shine acoustic")]
    assert shine.video_id == "b" and shine.evidence["paid_status"] == "paid_cooldown" and shine.evidence["demand_source"] == "public_proxy"
    # Proxy-only: gedeckelt, nicht handlungsauslösend.
    assert shine.evidence["evidence_level"] == "probe" and shine.evidence["actionable"] is False
    assert shine.scores["search_opportunity_score"] <= discovery.PROXY_SCORE_CAP
    demand = next(c for c in shine.components["components"] if c["name"].startswith("Eigene Views aus diesem Suchbegriff"))
    assert demand["available"] is False and "Werbephase" in (demand["note"] or "")
    assert all(0 <= (r.scores.get("external_audience_score") or 0) <= 100 for r in rows.values())
    assert all("keine Wahrscheinlichkeit" in (r.evidence.get("note") or "") for r in rows.values())
    best = discovery.best_for_video(session, "a")
    assert best and best["score"] is not None and best["gap"] != "insufficient_evidence"
    assert best["actionable"] is True and best["evidence_level"] == "own_analytics"
    view = discovery.overview(session, NOW)
    assert view["best"]["video_id"] in ("a", "b") and view["quota"]["units_used"] > 0 and view["capabilities"]
    assert view["best"]["evidence"]["actionable"] is True and view["actionable"] >= 1
    assert view["evidence_policy"]["proxy_score_cap"] == discovery.PROXY_SCORE_CAP
    assert view["signals"]["Ppg00gw0MxE" if "Ppg00gw0MxE" in view["signals"] else "a"]["demand_evidence"]["status"] in (
        "available", "unavailable_below_api_threshold", "no_search_traffic")
    assert any(c["status"] == "nicht genutzt" and "Trends" in c["source"] for c in view["capabilities"])
    assert view["signals"]["a"]["search_terms"][0]["term"] == "train journey music"


def test_external_opportunity_steers_v5_action_but_never_overrides_protection():
    base = {"status": "ok", "medians": {}}
    f = {"ctr_7d": None, "age_days": 400}
    strong = {"score": 72, "kind": "search", "key": "train journey music", "gap": "existing_video_opportunity",
              "demand_source": "own_analytics", "evidence_level": "own_analytics", "actionable": True, "audience": "train journey music"}
    action, notes = ge.choose_action("observe", f, {"signals": []}, base, [], {}, strong)
    assert action == "target_search_opportunity" and "own_analytics" in notes[0]
    # Proxy-only Chance (keine unabhaengige Evidenz) loest niemals eine aktive Massnahme aus.
    assert ge.choose_action("observe", f, {"signals": []}, base, [], {}, {**strong, "actionable": False, "evidence_level": "probe"})[0] == "observe"
    assert ge.choose_action("needs_discovery", f, {"signals": []}, base, [], {}, {**strong, "gap": "suggested_opportunity", "kind": "suggested"})[0] == "target_suggested_cluster"
    assert ge.choose_action("needs_packaging_test", f, {"signals": []}, base, [], {}, {**strong, "gap": "packaging_opportunity"})[0] == "packaging_for_audience"
    assert ge.choose_action("revival_candidate", f, {"signals": ["x", "y"]}, base, [], {}, strong)[0] == "revive_existing_video"
    assert ge.choose_action("observe", f, {"signals": []}, base, [], {}, {**strong, "gap": "followup_content_opportunity"})[0] == "create_followup_content"
    assert ge.choose_action("protect_momentum", f, {"signals": []}, base, [], {}, strong)[0] == "protect_no_change"
    assert ge.choose_action("paid_cooldown", {**f, "paid": {"days_until_clean": 5}}, {"signals": []}, base, [], {}, strong)[0] == "observe"
    assert ge.choose_action("observe", f, {"signals": []}, base, [], {}, {**strong, "score": 40})[0] == "observe"
    assert ge.choose_action("observe", f, {"signals": []}, base, [], {}, {**strong, "gap": "insufficient_evidence"})[0] == "observe"
    assert ge.choose_action("observe", f, {"signals": []}, base, [{"status": "registered", "decision_id": 3}], {}, strong)[0] == "observe"
    details = ge.action_details("target_search_opportunity", "observe", f, {"regime": "stable"}, base, {}, {"candidate": False}, None,
                                {"level": "low", "n_videos": 3}, notes, [], strong)
    assert details["audience"]["target"] == "train journey music" and details["window_days"] == 28 and details["target_metric"] == "discovery_views_7d"
    assert details["success_criterion"] and details["stop_criterion"] and "train journey music" in details["reason"]
    assert details["experiment_template"]["horizon_hours"] == 720 and "Thumbnail" in details["do_not_change"]


def test_growth_plan_combines_internal_and_external_signals(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=500)
    seed_history(session, "b", days=500, base=60, seed=2)
    discovery.run(DiscoveryClient(), NOW)
    learning.refresh(session, NOW)
    session.expire_all()
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    assert plan["internal_signals"]["state"] and "combined_decision" in plan and plan["external_signals"]["note"]
    row = next(r for r in plan["ranking"] if r["video_id"] == "a")
    assert row["external"] and row["external"]["score"] is not None and row["external"]["demand_source"] in ("own_analytics", "public_proxy")
    comp = next(c for c in learning.overview(session)["videos"]["a"]["recommendation"]["confidence"].items())
    assert plan["confidence"] in ("low", "insufficient_data")  # external public data never raises n_videos / confidence
    detail = session.scalar(select(GrowthPlan)).plan["ranking"][0]
    assert detail["priority"] == 1


def test_memory_scores_opportunities_after_window_and_weights_stay_bounded(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400)
    seed_history(session, "b", days=400, base=60, seed=2)
    old = TODAY-timedelta(days=discovery.MEMORY_DAYS+1)
    for i, (kind, key, baseline) in enumerate([("search", "train journey", 10), ("search", "shine acoustic", 30), ("suggested", "ext00000001", 0), ("cluster", "train", 0)]):
        session.add(DiscoveryOpportunity(day=old, kind=kind, key=key, video_id="a" if kind != "cluster" else None, gap="search_opportunity",
                                         scores={"external_audience_score": 60}, components={}, evidence={"baseline_views_for_memory": baseline}, status="open"))
    session.commit()
    discovery.run(DiscoveryClient(), NOW)
    session.expire_all()
    rows = {(r.kind, r.key): r for r in session.scalars(select(DiscoveryOpportunity).where(DiscoveryOpportunity.day == old))}
    assert rows[("search", "train journey")].outcome == "positive"   # 10 → 40 own search views
    assert rows[("search", "shine acoustic")].outcome == "negative"        # 30 → 12 (matched to a? evaluated on own totals)
    assert rows[("suggested", "ext00000001")].outcome == "positive"        # 0 → 25
    assert rows[("cluster", "train")].outcome == "inconclusive"
    assert all(r.status == "evaluated" and "nicht kausal" in (r.evaluation.get("detail") or "") or r.outcome != "positive" for r in rows.values())
    weights, record = discovery.memory_weights(session)
    assert record["search"]["n"] == 2 and weights == {}  # fewer than five decided outcomes: no prior shift
    for i in range(5):
        session.add(DiscoveryOpportunity(day=old-timedelta(days=i+1), kind="search", key=f"k{i}", gap="search_opportunity", scores={}, components={}, evidence={},
                                         status="evaluated", outcome="positive"))
    session.commit()
    weights, _ = discovery.memory_weights(session)
    assert 0.85 <= weights["search"] <= 1.15


def test_sync_runs_discovery_once_per_day_and_never_breaks_core(monkeypatch, session):
    from test_import_loop import FakeYouTube, wire as wire_sync
    wire_sync(monkeypatch, session)
    monkeypatch.setattr(discovery, "Session", sessionmaker(session.bind, expire_on_commit=False))
    monkeypatch.setattr(history.settings, "analytics_lag_days", LAG)
    assert pipeline.collect(FakeYouTube())["status"] == "ok"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(DiscoveryRun)) == 0  # plain fake has no public lookups
    class SyncClient(FakeYouTube, DiscoveryClient):
        def __init__(self):
            FakeYouTube.__init__(self)
            DiscoveryClient.__init__(self)
    client = SyncClient()
    assert pipeline.collect(client)["status"] == "ok"
    assert pipeline.collect(client)["status"] == "ok"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(DiscoveryRun)) == 1
    def boom(*args, **kwargs):
        raise RuntimeError("private-detail")
    monkeypatch.setattr(discovery, "run", boom)
    result = pipeline.collect(client)
    assert result["status"] == "ok" and any(i.startswith("optional/discovery: RuntimeError") for i in result["issues"])
    assert "private-detail" not in " ".join(result["issues"])


def test_client_methods_are_read_only_and_paginate_ids():
    from app.youtube import YouTube
    from pathlib import Path
    source = Path("app/youtube.py").read_text(encoding="utf-8")
    assert "search().list(" in source and "videos().list(" in source and "channels().list(" in source
    assert not any(token in source for token in ("videos().update(", "videos().insert(", "thumbnails().set(", "comments().insert(", "commentThreads().insert("))
    client = YouTube.__new__(YouTube)
    calls = []
    class Videos:
        def list(self, **kwargs):
            calls.append(kwargs["id"].split(","))
            return Mock(execute=lambda num_retries=0: {"items": [{"id": i} for i in kwargs["id"].split(",")]})
    client.data = Mock(videos=lambda: Videos())
    client.budget = None
    rows = client.videos_by_id([f"v{i}" for i in range(120)]+["v0"])
    assert len(rows) == 120 and [len(c) for c in calls] == [50, 50, 20]
