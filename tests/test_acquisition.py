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
from app.models import (AudiencePool, DiscoveryChannel, DiscoveryItem, DiscoverySignal, GrowthAction, TrafficDaily,
                        TrafficSurface, Video, VideoProfile, utcnow)
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


def own_theme(session, video_id="a"):
    """Unser Thema in eigenen Daten – ohne das gibt es keinen Audience-Fit zu pruefen."""
    video = session.get(Video, video_id)
    video.title = "Sealand Trainstories night train"
    video.duration_seconds = 214
    session.merge(VideoProfile(video_id=video_id, description="Ambient aus dem Nachtzug.",
                               tags=["night train ambient", "ambient"],
                               topics=["https://en.wikipedia.org/wiki/Ambient_music"], category_id="10",
                               channel_title="Sealand", channel_description="", channel_keywords="",
                               channel_topics=[], fetched_day=TODAY))
    session.commit()


def new_audience_channel(session, key="UCnew", title="Night Ambient Radio", subscribers=40000, views=900000):
    """Ein neu gefundener Kanal, der uns noch nie Zuschauer geschickt hat: ein erlaubtes Ziel.

    Bestehende Quellen – Radios, Redaktionen, einbettende Seiten, empfehlende Kanaele – sind nach der
    Produktregel geschuetzt und tauchen hier bewusst nicht als Aktion auf.
    """
    row = session.scalar(select(AudiencePool).where(AudiencePool.kind == "channel", AudiencePool.key == key))
    if row is None:
        row = AudiencePool(kind="channel", key=key, url=f"https://www.youtube.com/channel/{key}",
                           first_seen_day=TODAY, last_seen_day=TODAY)
        session.add(row)
    row.title, row.channel_id, row.channel_title = title, key, title
    row.item_count, row.subscribers, row.views = 200, subscribers, views
    row.description, row.query = "ambient night train music radio", "night train ambient"
    row.details = {"topics": ["https://en.wikipedia.org/wiki/Ambient_music"],
                   "keywords": "ambient night train", "query_source": "topic_context"}
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
    own_theme(session)
    new_audience_channel(session)
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.status == "proposed" and row.traffic_source == "YT_OTHER_PAGE"
    assert row.lever_class == "community_participation"
    assert row.target_metric == "other_views_7d" and row.surface_key == "UCnew"
    payload = row.payload
    assert payload["surface_title"] == "Night Ambient Radio"                   # wo
    assert "Abonnenten" in payload["why"]                                      # warum
    assert payload["steps"] and any("kommentier" in step.lower() for step in payload["steps"])   # was genau
    assert "YT_OTHER_PAGE" in payload["primary_metric"]                        # wie gemessen
    assert payload["mechanism"] and payload["window_days"] == aq.WINDOW_DAYS
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
    assert "Automatisches Posten" in " ".join(view["capabilities"]["not_used"])
    # Externe Web-Suchanbieter sind verworfen: sie duerfen nicht als genutzte Quelle auftauchen.
    assert not any("Web-Suche" in x for x in view["capabilities"]["used"])
    assert any("Web-Such" in x for x in view["capabilities"]["not_used"])
    # Die Pool-Suche berichtet ueber sich selbst, auch wenn sie nichts gefunden hat.
    assert view["pools"]["searched"] is False and view["pools"]["candidates"] == []


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


def test_a_running_discovery_experiment_blocks_what_it_cannot_separate(session):
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="link_from_own_video", target_metric="discovery_views_7d",
                             window_days=14, evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    own_theme(session)
    new_audience_channel(session)
    signal(session, "a", "own_search_term", "train journey music", 33)
    aq.collect(session, NOW, http=FakeHttp())
    result = aq.propose(session, NOW)
    rows = list(session.scalars(select(GrowthAction).where(GrowthAction.version == aq.VERSION)))
    # discovery_views_7d fasst alle YouTube-Oberflaechen zusammen: nichts davon ist davon trennbar.
    assert rows == [] and result["proposed"] == 0
    assert any("nicht trennbar" in b["reason"].lower() for b in result["blocked"])
    # Das laufende Experiment bleibt unangetastet.
    running = session.scalar(select(GrowthAction).where(GrowthAction.version == ge.VERSION))
    assert running.status == "running" and running.evaluate_after == TODAY+timedelta(days=16)


def test_starting_an_acquisition_action_uses_the_existing_lifecycle(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    own_theme(session)
    new_audience_channel(session)
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
                             state="needs_distribution", action="link_from_own_video", target_metric="impressions_7d",
                             window_days=14, evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    own_theme(session)
    new_audience_channel(session)
    result = aq.run(session, NOW, http=FakeHttp())
    assert result["issues"] == [] and result["surfaces"] >= 1 and result["proposed"] == 1
    view = aq.overview(session, NOW)
    entry = view["traffic_queue"][0]
    assert entry["video_id"] == "a" and entry["traffic_source"] == "YT_OTHER_PAGE"
    assert entry["surface"] == "Night Ambient Radio"
    assert entry["audience_fit"]["class"] in ("genre", "topic") and entry["steps"] and entry["mechanism"]
    assert entry["primary_metric"].startswith("zusätzliche qualifizierte Views")
    assert view["scoreboard"]["proposed"] == 1
    assert any(b["blocked_by"] for b in view["blocked"]) is False or view["blocked"] == []


def test_a_second_pass_never_overwrites_an_open_proposal(session):
    signal(session, "a", "own_external", "musikblog.example", 14)
    signal(session, "a", "own_search_term", "train journey music", 33)
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    first = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    before = (first.id, first.surface_key, first.action)
    assert aq.propose(session, NOW)["proposed"] == 0, "der offene Vorschlag bleibt stehen"
    session.expire_all()
    rows = list(session.scalars(select(GrowthAction).where(GrowthAction.version == aq.VERSION)))
    assert len(rows) == 1 and (rows[0].id, rows[0].surface_key, rows[0].action) == before


# ---------------------------------------------------------------------------- Lehren aus dem ersten Produktionslauf
def test_a_sharing_platform_is_evidence_but_never_an_outreach_target(session):
    """whatsapp.com kann man nicht kontaktieren – der erste Produktionslauf hätte das vorgeschlagen."""
    signal(session, "a", "own_external", "whatsapp.com", 30)
    aq.collect(session, NOW, http=FakeHttp())
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "own_external_referrer"))
    assert surface is not None, "als Beleg bleibt die Fläche sichtbar"
    assert surface.access["actionable"] is False
    assert "Teilen-Plattform" in surface.access["why_not"] and "privat weitergeben" in surface.access["why_not"]
    assert aq.propose(session, NOW)["proposed"] == 0
    assert session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION)) is None


def test_an_existing_source_is_never_a_task_however_much_traffic_it_sends(session):
    """PRODUKTREGEL: eine Seite, die uns schon Zuschauer schickt, ist eine gewachsene Beziehung.

    Sie wird gemessen und fliesst in das Audience-Lernen ein, aber dieses System spricht sie nie an –
    weder bei einem View noch bei tausend.
    """
    signal(session, "a", "own_external", "kleinblog.example", 1)
    signal(session, "a", "own_external", "radiosender.example", 4000)
    signal(session, "a", "own_embed", "bahnblog.example/nachtzug", 120)
    aq.collect(session, NOW, http=FakeHttp())
    surfaces = list(session.scalars(select(TrafficSurface)))
    assert surfaces, "die Quellen bleiben als Messsignal sichtbar"
    for surface in surfaces:
        assert surface.access["actionable"] is False
        assert surface.access.get("protected") is True
        assert surface.access["protected_reason"] == "protected_existing_source"
        assert "nie angesprochen" in surface.access["why_not"]
    assert aq.propose(session, NOW)["proposed"] == 0
    # Erst ein neu gefundener Ort, der uns noch nie Zuschauer geschickt hat, wird eine Aufgabe.
    own_theme(session)
    new_audience_channel(session)
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.surface_key == "UCnew" and row.traffic_source == "YT_OTHER_PAGE"


def test_an_impressions_experiment_does_not_block_external_outreach(session):
    """Der Produktionsfall: #8 misst impressions_7d. Externe Links erzeugen Views, aber keine Impressions."""
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence", target_metric="impressions_7d",
                             window_days=14, evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    session.commit()
    allowed, reason = aq.blocking(session, "a", "community_participation", "YT_OTHER_PAGE", "other_views_7d")
    assert allowed is None, "ein Klick von einer fremden Seite erzeugt keine Impression auf unserer Oberflaeche"
    blocked, reason = aq.blocking(session, "a", "community_participation", "RELATED_VIDEO", "suggested_views_7d")
    assert blocked is not None and "impressions_7d" in reason
    own_theme(session)
    new_audience_channel(session)
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.traffic_source == "YT_OTHER_PAGE" and row.video_id == "a"


def candidate(session, video_id, channel_id, title, views, subscribers=5000, channel_title="Rail Sounds"):
    session.add(DiscoveryItem(video_id=video_id, channel_id=channel_id, title=title, channel_title=channel_title,
                              views=views, tags=[], via={"queries": ["train journey"]}, first_seen_day=TODAY,
                              last_seen_day=TODAY, seen_count=1))
    if not session.get(DiscoveryChannel, channel_id):
        session.add(DiscoveryChannel(channel_id=channel_id, title=channel_title, subscribers=subscribers,
                                     video_count=50, views=views*10, first_seen_day=TODAY, last_seen_day=TODAY))


def test_new_surfaces_need_corroboration_across_channels(session):
    session.get(Video, "a").title = "Sealand Trainstories night train"
    candidate(session, "CAND0000001", "UC1", "Night train journey ambient", 50000, channel_title="Rail One")
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    assert not list(session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "candidate_video"))), \
        "ein einzelner Treffer ist Zufall, kein Thema"
    candidate(session, "CAND0000002", "UC2", "Night train journey through europe", 90000, channel_title="Rail Two")
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    rows = {r.key: r for r in session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "candidate_video"))}
    assert set(rows) == {"CAND0000001", "CAND0000002"}
    surface = rows["CAND0000002"]
    assert surface.video_id == "a" and surface.traffic_source == "YT_OTHER_PAGE"
    assert surface.evidence["channels"] == 2 and surface.evidence["members"] == 2
    assert surface.evidence["demand_source"] == "public_proxy_corroborated"
    assert "90000 öffentlich gezählte Views" in surface.evidence["why"]
    assert "noch nicht" in surface.evidence["why"] and "mittel" in surface.evidence["uncertainty"]
    assert surface.scores["expected_weekly_views"] is None, "kein gemessener eigener Traffic, also keine Prognose"
    channels = {r.key for r in session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "candidate_channel"))}
    assert channels == {"UC1", "UC2"}


def test_a_new_surface_ranks_below_a_proven_one_and_offers_participation(session):
    session.get(Video, "a").title = "Sealand Trainstories night train"
    candidate(session, "CAND0000001", "UC1", "Night train journey ambient", 500000, channel_title="Rail One")
    candidate(session, "CAND0000002", "UC2", "Night train journey europe", 400000, channel_title="Rail Two")
    signal(session, "a", "own_external", "musikblog.example", 40)
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    proven = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "own_external_referrer"))
    best_candidate = max(session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "candidate_video")),
                         key=lambda s: s.scores["traffic_potential"])
    assert proven.scores["traffic_potential"] > best_candidate.scores["traffic_potential"], \
        "gemessener eigener Traffic schlaegt oeffentlich belegte Reichweite"
    steps = aq.steps_for("candidate_video", best_candidate, "Trainstories")
    assert any("ansehen" in s for s in steps) and any("kein Link" in s for s in steps)
    assert "YT_OTHER_PAGE" in aq.mechanism("candidate_video", best_candidate)


def test_a_proposal_is_withdrawn_when_its_surface_disappears(session):
    own_theme(session)
    new_audience_channel(session)
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    # Am naechsten Tag ist der Kanal nicht mehr unter den gefundenen Orten.
    for surface in session.scalars(select(TrafficSurface)):
        session.delete(surface)
    session.query(AudiencePool).delete()
    session.commit()
    later = NOW+timedelta(days=1)
    aq.collect(session, later, http=FakeHttp())
    result = aq.propose(session, later)
    session.expire_all()
    withdrawn = session.get(GrowthAction, row.id)
    assert withdrawn.status == "superseded" and withdrawn.outcome == "inconclusive"
    assert "nicht mehr auf" in withdrawn.evaluation["reason"]
    assert "nie ausgeführt" in withdrawn.evaluation["note"]
    assert [d["action_id"] for d in result["dropped"]] == [row.id]
    assert aq.overview(session, later)["traffic_queue"] == []


def test_the_theme_vocabulary_comes_from_the_neighbourhood_not_only_the_own_title(session):
    """„Sealand Trainstories“ ergibt ein einziges Wort – die Nachbarvideos beschreiben das Thema belegt."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.add(DiscoveryItem(video_id="NEIGHBOUR01", channel_id="UCN", title="Night train ambient journey",
                              channel_title="Rail Nights", views=120000, tags=[],
                              via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY, last_seen_day=TODAY,
                              seen_count=1))
    session.commit()
    thin = aq.own_vocabulary(session)
    assert thin["a"] == {"trainstories"}, "aus dem Titel allein wird nichts"
    signal(session, "a", "own_suggested_source", "NEIGHBOUR01", 3)
    rich = aq.own_vocabulary(session)
    assert {"night", "train", "ambient", "journey"} <= rich["a"], "die belegte Nachbarschaft liefert das Thema"
    # Damit findet die Kandidatensuche ueberhaupt etwas.
    candidate(session, "CAND0000001", "UC1", "Night train journey relaxing", 60000, channel_title="Rail One")
    candidate(session, "CAND0000002", "UC2", "Ambient night train ride", 80000, channel_title="Rail Two")
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    found = {r.key for r in session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "candidate_video"))}
    assert found == {"CAND0000001", "CAND0000002"}


def test_format_words_are_not_a_shared_theme(session):
    """Der zweite Produktionslauf schlug ein BLACKPINK-Teaser als Fläche für unser Teaser-Video vor."""
    session.get(Video, "a").title = "11AM Album - Teaser"
    candidate(session, "CAND0000001", "UC1", "LISA - FIRST SINGLE ALBUM LALISA VISUAL TEASER #3", 900000,
              channel_title="BLACKPINK")
    candidate(session, "CAND0000002", "UC2", "Skrillex -- Recess Album Teaser Video", 500000, channel_title="Skrillex")
    session.commit()
    assert {"album", "teaser"} <= aq.generic_tokens(session), "Formatwoerter werden erkannt"
    aq.collect(session, NOW, http=FakeHttp())
    assert not list(session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "candidate_video"))), \
        "gemeinsame Formatwoerter sind kein Thema"
    assert aq.propose(session, NOW)["proposed"] == 0
    # Echte Themenbegriffe bleiben wirksam.
    session.get(Video, "a").title = "Sealand night train ambient"
    candidate(session, "CAND0000003", "UC3", "Night train ambient journey", 60000, channel_title="Rail One")
    candidate(session, "CAND0000004", "UC4", "Ambient night train ride", 70000, channel_title="Rail Two")
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    found = {r.key for r in session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "candidate_video"))}
    assert found == {"CAND0000003", "CAND0000004"}


def test_a_surface_without_a_traffic_path_is_not_blamed_on_a_running_experiment(session):
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence", target_metric="impressions_7d",
                             window_days=14, evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    signal(session, "a", "own_suggested_source", "EXTVIDEO123", 1)
    session.add(DiscoveryItem(video_id="EXTVIDEO123", channel_id="UCX", title="Irgendein Video", channel_title="X",
                              views=100, tags=[], via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY,
                              last_seen_day=TODAY, seen_count=1))
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    view = aq.overview(session, NOW)
    assert view["blocked"] == [], "eine Fläche mit einem View ist kein Konflikt, sondern fehlende Evidenz"
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "recommending_video"))
    assert surface.access["actionable"] is False


def test_todays_surfaces_are_rebuilt_so_a_retired_one_disappears(session):
    session.get(Video, "a").title = "Sealand night train ambient"
    candidate(session, "CAND0000003", "UC3", "Night train ambient journey", 60000, channel_title="Rail One")
    candidate(session, "CAND0000004", "UC4", "Ambient night train ride", 70000, channel_title="Rail Two")
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    # Die Kandidaten verschwinden aus der Datenlage (z. B. weil die Regeln verschaerft wurden).
    for item in list(session.scalars(select(DiscoveryItem))):
        session.delete(item)
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    assert not list(session.scalars(select(TrafficSurface))), "der heutige Stand wird neu erhoben"
    aq.propose(session, NOW)
    session.expire_all()
    assert session.get(GrowthAction, row.id).status == "superseded"
    assert aq.overview(session, NOW)["traffic_queue"] == []


def test_a_surface_with_almost_no_measurable_inflow_is_evidence_not_a_task(session):
    """Ein einziger View in 90 Tagen sind ~0,08 Views/Woche: das darf nicht unsere wichtigste Aktion sein."""
    session.add(DiscoveryItem(video_id="NB123456789", channel_id="UCM", title="Anii cei mai dragi din viata mea",
                              channel_title="Mihai Ciobanu (Oficial)", views=180000, tags=[],
                              via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY, last_seen_day=TODAY,
                              seen_count=1))
    session.add(DiscoveryChannel(channel_id="UCM", title="Mihai Ciobanu (Oficial)", subscribers=95000,
                                 video_count=300, views=50000000, first_seen_day=TODAY, last_seen_day=TODAY))
    signal(session, "b", "own_suggested_source", "NB123456789", 1)
    session.commit()
    aq.collect(session, NOW, http=FakeHttp())
    rows = {r.kind: r for r in session.scalars(select(TrafficSurface))}
    channel = rows["recommending_channel"]
    assert channel.access["actionable"] is False and channel.access.get("protected") is True
    assert "nie angesprochen" in channel.access["why_not"]
    assert channel.scores["subscribers"] == 95000, "die Fläche wird weiter gemessen"
    assert rows["recommending_video"].access["actionable"] is False
    # Nichts davon darf in JETZT TUN landen.
    assert aq.propose(session, NOW)["proposed"] == 0
    view = aq.overview(session, NOW)
    assert view["traffic_queue"] == []
    assert any(s["title"] == "Mihai Ciobanu (Oficial)" for s in view["surfaces"]), "als Fläche weiter sichtbar"
    # Ohne oeffentliche Reichweite bleibt ein einzelner View eine blosse Beobachtung.
    session.query(DiscoveryChannel).delete()
    session.query(DiscoveryItem).delete()
    session.add(DiscoveryItem(video_id="NB123456789", channel_id="UCM", title="Kleines Video", channel_title="X",
                              views=0, tags=[], via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY,
                              last_seen_day=TODAY, seen_count=1))
    session.commit()
    later = NOW+timedelta(days=1)
    aq.collect(session, later, http=FakeHttp())
    weak = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "recommending_video",
                                                      TrafficSurface.day == ge.pacific_day(later)))
    assert weak.access["actionable"] is False


def test_an_open_proposal_is_updated_or_replaced_by_a_better_surface(session):
    own_theme(session)
    new_audience_channel(session, key="UCsmall", title="Small Ambient Radio", subscribers=500, views=1000)
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert "500 Abonnenten" in row.payload["why"]
    # Derselbe Ort waechst: der Text muss der Messung folgen.
    new_audience_channel(session, key="UCsmall", title="Small Ambient Radio", subscribers=520, views=1100)
    later = NOW+timedelta(days=1)
    aq.collect(session, later, http=FakeHttp())
    aq.propose(session, later)
    session.expire_all()
    updated = session.get(GrowthAction, row.id)
    assert updated.status == "proposed" and "520 Abonnenten" in updated.payload["why"], "Text folgt der Messung"
    # Eine klar bessere Flaeche ersetzt den Vorschlag.
    new_audience_channel(session, key="UCbig", title="Big Ambient Radio", subscribers=900000, views=200000000)
    session.commit()
    even_later = NOW+timedelta(days=2)
    aq.collect(session, even_later, http=FakeHttp())
    result = aq.propose(session, even_later)
    session.expire_all()
    assert session.get(GrowthAction, row.id).status == "superseded"
    assert "Bessere Fläche gefunden" in session.get(GrowthAction, row.id).evaluation["reason"]
    assert result["proposed"] == 1
    fresh = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION,
                                                     GrowthAction.status == "proposed"))
    assert fresh.id != row.id and fresh.payload["surface_title"] == "Big Ambient Radio"


def test_a_withdrawn_proposal_does_not_block_a_better_one_on_the_same_day(session):
    """Sonst bliebe die Queue bis zum naechsten Tag leer, obwohl eine handelbare Flaeche vorliegt."""
    own_theme(session)
    signal(session, "a", "own_external", "kleinblog.example", 1)          # geschuetzte bestehende Quelle
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 0
    # Spaeter am selben Tag liegt ein neu gefundener Ort vor.
    new_audience_channel(session)
    aq.collect(session, NOW, http=FakeHttp())
    result = aq.propose(session, NOW)
    assert result["proposed"] == 1, "die bessere Flaeche kommt noch heute in die Queue"
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.status == "proposed" and row.surface_key == "UCnew"
    assert row.outcome is None and row.evaluation is None
    assert aq.overview(session, NOW)["traffic_queue"][0]["surface"] == "Night Ambient Radio"
