"""Acquisition-Layer: echte Trafficquellen finden, ansprechen und den Traffic daraus messen.

Gemessen wird dieses System an zusaetzlichen qualifizierten Views, nicht an Analysen. Die Tests pruefen
deshalb: nur belegte Flaechen, ausfuehrbare Aktionen, Attribution je Quelle, Verstaerken was wirkt,
Verwerfen was nicht wirkt – und dass ein laufendes Experiment nur blockiert, was nicht trennbar ist.
"""
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import acquisition as aq, growth_engine as ge, main
from app.models import (DiscoveryChannel, DiscoveryItem, DiscoverySignal, GrowthAction, TrafficDaily, TrafficSurface,
                        Video, utcnow)
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG

HEADERS = {"Authorization": "Bearer test-token-only"}
WINDOW_END = TODAY-timedelta(days=LAG)


class FakeHttp:
    """Statt echter Netzabfragen: welche Seite antwortet wie."""
    def __init__(self, status=200, statuses=None):
        self.status, self.statuses, self.calls = status, statuses or {}, []

    def head(self, url):
        self.calls.append(url)
        return type("R", (), {"status_code": self.statuses.get(url, self.status)})()

    get = head


def signal(session, video_id, kind, detail, views, minutes=10.0, end=WINDOW_END):
    session.add(DiscoverySignal(video_id=video_id, kind=kind, detail=detail, window_start=end-timedelta(days=89),
                                window_end=end, views=views, watch_minutes=minutes, fetched_day=end))
    session.commit()


def traffic(session, video_id, source, day, views, paid=False, minutes=2.0):
    session.add(TrafficDaily(video_id=video_id, day=day, source=source, views=views, watch_minutes=minutes,
                             paid=paid, fetched_at=NOW))


# ---------------------------------------------------------------------------- Flächen finden
def test_only_verifiable_places_with_measured_audience_become_surfaces(session):
    signal(session, "a", "own_external", "musikblog.example", 14)
    signal(session, "a", "own_external", "tot.example", 9)
    signal(session, "a", "own_external", "other", 5)                 # kein Ort, nur ein Platzhalter
    http = FakeHttp(statuses={"https://musikblog.example": 200, "https://tot.example": 404})
    result = aq.collect(session, NOW, http=http)
    rows = {r.key: r for r in session.scalars(select(TrafficSurface))}
    assert "https://musikblog.example" in rows and "https://tot.example" not in rows, "tote Seite ist keine Flaeche"
    assert not any("other" in k for k in rows), "Platzhalter ohne pruefbare Quelle wird verworfen"
    surface = rows["https://musikblog.example"]
    assert surface.traffic_source == "EXT_URL" and surface.lever_class == "external_outreach"
    assert surface.http_status == 200 and surface.verified_at is not None
    assert surface.evidence["measured_views_90d"] == 14 and surface.evidence["demand_source"] == "own_analytics"
    assert "14 Views" in surface.evidence["why"] and "musikblog.example" in surface.evidence["why"]
    assert surface.scores["expected_weekly_views"] > 0 and 0 <= surface.scores["traffic_potential"] <= 100
    assert result["surfaces"] >= 1 and result["verified"] >= 1


def test_recommending_videos_channels_and_real_search_terms_are_surfaces(session):
    session.add(DiscoveryItem(video_id="EXTVIDEO123", channel_id="UCX", title="Night Train Ambient",
                              channel_title="Rail Sounds", views=90000, tags=[], via={"suggested_source": ["own_traffic"]},
                              first_seen_day=TODAY, last_seen_day=TODAY, seen_count=1))
    session.add(DiscoveryChannel(channel_id="UCX", title="Rail Sounds", subscribers=42000, video_count=120,
                                 views=9000000, first_seen_day=TODAY, last_seen_day=TODAY))
    signal(session, "a", "own_suggested_source", "EXTVIDEO123", 21)
    signal(session, "a", "own_search_term", "train journey music", 33)
    signal(session, "a", "own_search_term", "trainstories", 8)        # ein Wort: keine Suchintention
    aq.collect(session, NOW, http=FakeHttp())
    rows = {(r.kind, r.key): r for r in session.scalars(select(TrafficSurface))}
    video = rows[("recommending_video", "EXTVIDEO123")]
    assert video.title == "Night Train Ambient" and video.url.endswith("EXTVIDEO123")
    assert "21 mal neben" in video.evidence["why"] and video.traffic_source == "RELATED_VIDEO"
    channel = rows[("recommending_channel", "UCX")]
    assert channel.evidence["subscribers"] == 42000 and "Rail Sounds" in channel.evidence["why"]
    intent = rows[("own_search_intent", "train journey music")]
    assert intent.traffic_source == "YT_SEARCH" and "33 Zuschauer" in intent.evidence["why"]
    assert ("own_search_intent", "trainstories") not in rows


# ---------------------------------------------------------------------------- Priorisierung
def test_measured_audience_outranks_analytical_interest():
    strong = aq.score_surface(fit=1.0, present_views=40, access_score=0.8, effort=0.3, weight=1.0)
    thin = aq.score_surface(fit=1.0, present_views=1, access_score=0.8, effort=0.3, weight=1.0)
    no_access = aq.score_surface(fit=1.0, present_views=40, access_score=0.1, effort=0.9, weight=1.0)
    assert strong > thin, "gemessenes Publikum schlaegt blosse Relevanz"
    assert strong > no_access, "ohne realistischen Zugang keine Spitzenposition"
    assert aq.expected_weekly_views(90) == pytest.approx(7.0, abs=0.1)


# ---------------------------------------------------------------------------- Aktionen
def test_every_proposal_answers_where_why_what_and_how_measured(session):
    signal(session, "a", "own_external", "musikblog.example", 14)
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.status == "proposed" and row.traffic_source == "EXT_URL" and row.lever_class == "external_outreach"
    assert row.target_metric == "external_views_7d" and row.surface_key == "https://musikblog.example"
    payload = row.payload
    assert payload["surface_title"] == "musikblog.example"                     # wo
    assert "14 Views" in payload["why"]                                        # warum
    assert payload["steps"] and "Kontakt" in payload["steps"][0]               # was genau
    assert "EXT_URL" in payload["primary_metric"]                              # wie gemessen
    assert "Leser" in payload["mechanism"] and payload["window_days"] == aq.WINDOW_DAYS
    assert payload["executed_automatically"] is False and "postet nichts" in payload["note"]
    assert "Spam" in payload["rules"] and "Bots" in payload["rules"]
    assert "Titel" in payload["do_not_change"] and "Beschreibung" in payload["do_not_change"]


def test_the_traffic_queue_contains_no_observation_or_protection(session):
    signal(session, "a", "own_external", "musikblog.example", 14)
    signal(session, "b", "own_search_term", "shine on acoustic", 12)
    aq.collect(session, NOW, http=FakeHttp())
    aq.propose(session, NOW)
    view = aq.overview(session, NOW)
    assert view["traffic_queue"], "die Traffic-Queue ist gefuellt"
    for entry in view["traffic_queue"]:
        assert entry["action"] in aq.ACTION_LABELS
        assert entry["action"] not in ("observe", "probe_missing_evidence", "protect_no_change")
        assert entry["surface"] and entry["why"] and entry["steps"] and entry["primary_metric"]
        assert entry["confirm"]["endpoint"].endswith("/start")
    assert "Web-/Foren-/Blog-Suche" in " ".join(view["capabilities"]["not_used"])


# ---------------------------------------------------------------------------- Konflikte nach Hebel/Quelle
def test_a_running_internal_test_blocks_only_what_it_cannot_be_told_apart_from(session):
    # Das laufende Experiment #8: interne Verlinkung, gemessen an discovery_views_7d.
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="link_from_own_video", target_metric="discovery_views_7d",
                             window_days=14, evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    session.commit()
    # Externe Ansprache: andere Quelle, anderer Hebel, getrennt messbar.
    other, reason = aq.blocking(session, "a", "external_outreach", "EXT_URL", "external_views_7d")
    assert other is None and reason is None
    # Suchwortlaut: YT_SEARCH steckt in discovery_views_7d – nicht trennbar.
    other, reason = aq.blocking(session, "a", "search_wording", "YT_SEARCH", "search_views_7d")
    assert other is not None and "discovery_views_7d" in reason and "YT_SEARCH" in reason
    # Gleicher Hebel: ebenfalls gesperrt.
    other, reason = aq.blocking(session, "a", "internal_link", "PLAYLIST", "discovery_views_7d")
    assert other is not None and "selben Hebel" in reason


def test_a_separable_external_action_is_offered_while_the_internal_test_runs(session):
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="link_from_own_video", target_metric="discovery_views_7d",
                             window_days=14, evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    signal(session, "a", "own_external", "musikblog.example", 14)
    signal(session, "a", "own_search_term", "train journey music", 33)
    aq.collect(session, NOW, http=FakeHttp())
    result = aq.propose(session, NOW)
    rows = list(session.scalars(select(GrowthAction).where(GrowthAction.version == aq.VERSION)))
    assert [r.traffic_source for r in rows] == ["EXT_URL"], "nur die trennbare Quelle wird vorgeschlagen"
    assert any(b["traffic_source"] == "YT_SEARCH" and "nicht trennbar" in b["reason"].lower() for b in result["blocked"])
    # Das laufende Experiment bleibt unangetastet.
    running = session.scalar(select(GrowthAction).where(GrowthAction.version == ge.VERSION))
    assert running.status == "running" and running.evaluate_after == TODAY+timedelta(days=16)


def test_starting_an_acquisition_action_uses_the_existing_lifecycle(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    signal(session, "a", "own_external", "musikblog.example", 14)
    aq.collect(session, NOW, http=FakeHttp())
    aq.propose(session, NOW)
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    main.app.dependency_overrides[main.db] = lambda: session
    try:
        client = TestClient(main.app)
        response = client.post(f"/api/growth/actions/{row.id}/start", headers=HEADERS)
        assert response.status_code == 200 and response.json()["status"] == "running"
        assert response.json()["executed_automatically"] is False
        view = client.get("/api/acquisition", headers=HEADERS).json()
        assert view["traffic_queue"] == [] and view["running"][0]["action_id"] == row.id
        assert view["running"][0]["started_day"] == str(ge.pacific_day(utcnow()))
    finally:
        main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------- Messen und lernen
def test_the_effect_is_attributed_to_the_source_that_was_worked_on(session):
    start = TODAY-timedelta(days=20)
    row = GrowthAction(video_id="a", created_day=start, created_at=NOW, version=aq.VERSION, state="traffic_acquisition",
                       action="reach_out_to_referrer", target_metric="external_views_7d", window_days=14,
                       evaluate_after=start+timedelta(days=17), status="running", started_day=start, started_at=NOW,
                       lever_class="external_outreach", traffic_source="EXT_URL", surface_key="https://musikblog.example",
                       payload={"surface_kind": "own_external_referrer"}, baseline={})
    session.add(row)
    for i in range(1, 15):
        traffic(session, "a", "EXT_URL", start-timedelta(days=i), 1)          # vorher: 14 Views
        traffic(session, "a", "EXT_URL", start+timedelta(days=i), 4)          # nachher: 56 Views
        traffic(session, "a", "YT_SEARCH", start+timedelta(days=i), 50)       # andere Quelle zaehlt nicht mit
    session.commit()
    assert aq.evaluate(session, NOW) == 1
    session.expire_all()
    done = session.get(GrowthAction, row.id)
    attribution = done.evaluation["attribution"]
    assert done.status == "evaluated" and done.outcome == "positive"
    assert attribution["source"] == "EXT_URL" and attribution["views_before"] == 14 and attribution["views_after"] == 56
    assert attribution["views_delta"] == 42 and attribution["surface_key"] == "https://musikblog.example"
    assert "kein Kausalbeweis" in done.evaluation["note"]
    board = aq.scoreboard(session)
    assert board["attributed_views_delta"] == 42 and board["per_source"]["EXT_URL"]["views_delta"] == 42


def test_paid_traffic_makes_attribution_inconclusive(session):
    start = TODAY-timedelta(days=20)
    session.add(GrowthAction(video_id="a", created_day=start, created_at=NOW, version=aq.VERSION,
                             state="traffic_acquisition", action="reach_out_to_referrer",
                             target_metric="external_views_7d", window_days=14, evaluate_after=start+timedelta(days=17),
                             status="running", started_day=start, started_at=NOW, lever_class="external_outreach",
                             traffic_source="EXT_URL", surface_key="k", payload={"surface_kind": "own_external_referrer"},
                             baseline={}))
    traffic(session, "a", "ADVERTISING", start+timedelta(days=2), 30, paid=True)
    session.commit()
    aq.evaluate(session, NOW)
    session.expire_all()
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.outcome == "inconclusive" and "Werbetraffic" in row.evaluation["detail"]


def test_sources_that_deliver_are_strengthened_and_dead_ends_are_retired(session):
    def evaluated(kind, delta, day):
        session.add(GrowthAction(video_id="a", created_day=day, created_at=NOW, version=aq.VERSION,
                                 state="traffic_acquisition", action="reach_out_to_referrer",
                                 target_metric="external_views_7d", window_days=14, evaluate_after=day,
                                 status="evaluated", outcome="positive" if delta > 0 else "neutral",
                                 lever_class="external_outreach", traffic_source="EXT_URL", surface_key=f"k{day}",
                                 payload={"surface_kind": kind}, baseline={},
                                 evaluation={"attribution": {"views_delta": delta}}))
    for i in range(2):
        evaluated("own_external_referrer", 10, TODAY-timedelta(days=60+i))
    session.commit()
    learned = aq.weights(session)["own_external_referrer"]
    assert learned["weight"] == 1.0 and "nichts bewiesen" in learned["basis"], "unter drei Versuchen kein Urteil"
    evaluated("own_external_referrer", 25, TODAY-timedelta(days=50))
    session.commit()
    learned = aq.weights(session)["own_external_referrer"]
    assert learned["weight"] > 1.0 and learned["views_gained"] == 45
    assert aq.WEIGHT_RANGE[0] <= learned["weight"] <= aq.WEIGHT_RANGE[1]
    # Eine Quellenart, die dreimal nichts gebracht hat, wird verworfen und nicht erneut vorgeschlagen.
    for i in range(3):
        evaluated("recommending_channel", 0, TODAY-timedelta(days=40+i))
    session.commit()
    assert aq.weights(session)["recommending_channel"]["retired"] is True
    session.add(DiscoveryItem(video_id="EXTVIDEO123", channel_id="UCX", title="N", channel_title="Rail Sounds",
                              views=1000, tags=[], via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY,
                              last_seen_day=TODAY, seen_count=1))
    session.add(DiscoveryChannel(channel_id="UCX", title="Rail Sounds", subscribers=1000, video_count=5, views=10,
                                 first_seen_day=TODAY, last_seen_day=TODAY))
    signal(session, "a", "own_suggested_source", "EXTVIDEO123", 12)
    aq.collect(session, NOW, http=FakeHttp())
    kinds = {r.kind for r in session.scalars(select(TrafficSurface))}
    assert "recommending_channel" not in kinds and "recommending_video" in kinds


def test_the_scoreboard_reports_additional_views_not_activity(session):
    board = aq.scoreboard(session)
    assert board["attributed_views_delta"] == 0 and board["per_source"] == {}
    assert "kein Kausalbeweis" in board["note"]


# ---------------------------------------------------------------------------- Ende zu Ende
def test_production_shape_trainstories_gets_a_separable_traffic_action(monkeypatch, session):
    """Die Definition of Done: trotz laufendem internem Test entsteht eine ausfuehrbare Traffic-Aktion."""
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="link_from_own_video", target_metric="discovery_views_7d",
                             window_days=14, evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    signal(session, "a", "own_external", "trainspotting-forum.example/thread/42", 11)
    session.commit()
    result = aq.run(session, NOW, http=FakeHttp())
    assert result["issues"] == [] and result["surfaces"] >= 1 and result["proposed"] == 1
    view = aq.overview(session, NOW)
    entry = view["traffic_queue"][0]
    assert entry["video_id"] == "a" and entry["traffic_source"] == "EXT_URL"
    assert entry["surface"].startswith("trainspotting-forum.example")
    assert entry["verified"] is True and entry["steps"] and entry["mechanism"]
    assert entry["primary_metric"].startswith("zusätzliche qualifizierte Views")
    assert view["scoreboard"]["proposed"] == 1
    assert any(b["blocked_by"] for b in view["blocked"]) is False or view["blocked"] == []
