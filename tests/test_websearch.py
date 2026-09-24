"""Externe Audience-Suche: kostenloses Kontingent, harte Filter, Hypothese statt Behauptung.

Keyword-Uebereinstimmung allein reicht nicht: ein Treffer muss mehrere spezifische Begriffe mit unserem
Thema teilen, strukturell nach Community aussehen, erreichbar sein und darf nicht auf einer Handels-,
Streaming- oder Teilen-Plattform liegen.
"""
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import acquisition as aq, growth_engine as ge, main, websearch
from app.config import settings
from app.models import DiscoveryItem, GrowthAction, TrafficSurface, Video, WebSearchQuota
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG
from test_acquisition import FakeHttp, signal

HEADERS = {"Authorization": "Bearer test-token-only"}


class FakeSearch:
    """Ein Such-Provider, der genau die Treffer liefert, die der Test braucht."""

    def __init__(self, results, status=200, page_status=200):
        self.results, self.status, self.queries = results, status, []
        self.page_status = page_status

    def get(self, url, params=None):
        if params is None:                       # Erreichbarkeitspruefung einer Seite, keine Suche
            return type("R", (), {"status_code": self.page_status})()
        self.queries.append(params.get("q"))
        payload = {"items": [{"title": r[0], "link": r[1], "snippet": r[2]} for r in self.results]}
        return type("R", (), {"status_code": self.status, "json": lambda _self=None: payload})()

    def head(self, url):
        return type("R", (), {"status_code": self.page_status})()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "web_search_provider", "google_cse")
    monkeypatch.setattr(settings, "web_search_key", "test-key")
    monkeypatch.setattr(settings, "web_search_cx", "test-cx")
    monkeypatch.setattr(settings, "web_search_daily_limit", 50)
    return settings


def theme(session):
    """Ein Video mit belegtem Thema aus der Nachbarschaft, damit die Suche etwas zu suchen hat."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.add(DiscoveryItem(video_id="NEIGHBOUR01", channel_id="UCN", title="Night train ambient journey",
                              channel_title="Rail Nights", views=120000, tags=[],
                              via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY, last_seen_day=TODAY,
                              seen_count=1))
    session.commit()
    signal(session, "a", "own_suggested_source", "NEIGHBOUR01", 4)


def test_without_a_key_the_search_stays_off_and_says_what_to_configure(session):
    state = websearch.status()
    assert state["configured"] is False and state["setup"]
    assert any("Custom Search API" in step for step in state["setup"])
    assert any("WEB_SEARCH_PROVIDER" in step for step in state["setup"])
    with pytest.raises(websearch.SearchUnavailable):
        websearch.search(session, "irgendwas", TODAY)
    result = aq.collect(session, NOW, http=FakeHttp())
    assert result["web_search"]["queries"] == 0 and "WEB_SEARCH_PROVIDER" in result["web_search"]["reason"]


def test_the_daily_cap_is_counted_and_never_exceeded(session, configured, monkeypatch):
    monkeypatch.setattr(settings, "web_search_daily_limit", 2)
    client = FakeSearch([("Night train forum", "https://bahnforum.example/thread/12", "Night train ambient thread")])
    assert websearch.remaining(session, TODAY) == 2
    websearch.search(session, "a", TODAY, client=client)
    websearch.search(session, "b", TODAY, client=client)
    assert websearch.used_today(session, TODAY) == 2 and websearch.remaining(session, TODAY) == 0
    with pytest.raises(websearch.SearchUnavailable) as exc:
        websearch.search(session, "c", TODAY, client=client)
    assert "Tageslimit" in str(exc.value)
    assert len(client.queries) == 2, "nach dem Limit wird der Provider nicht mehr aufgerufen"
    assert session.get(WebSearchQuota, TODAY).queries == 2


def test_only_relevant_reachable_communities_survive_the_filter(session, configured):
    theme(session)
    client = FakeSearch([
        ("Night train ambient - Forum thread", "https://bahnforum.example/viewtopic.php?t=91",
         "Diskussion ueber night train ambient journey Musik, 240 posts, 1.2k members"),
        ("Night train ambient auf Spotify", "https://open.spotify.com/playlist/xyz", "night train ambient playlist"),
        ("Bahn-Reise Blog", "https://reiseblog.example/night-train-journey",
         "Blog ueber night train journey und ambient Musik unterwegs"),
        ("Irgendein Shop", "https://amazon.de/dp/B01", "night train ambient journey CD kaufen"),
        ("Promi-News", "https://klatschnews.example/artikel/123", "Ein Star reiste mit dem Zug"),
        ("Nur ein Wort", "https://forum.example/thread/1", "Hier geht es um train und sonst nichts Passendes"),
    ])
    result = aq.collect(session, NOW, http=client)
    kept = {r.key: r for r in session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "web_community"))}
    assert set(kept) == {"https://bahnforum.example/viewtopic.php?t=91",
                         "https://reiseblog.example/night-train-journey"}, list(kept)
    assert result["web_search"]["kept"] == 2 and result["web_search"]["queries"] > 0
    forum = kept["https://bahnforum.example/viewtopic.php?t=91"]
    assert forum.traffic_source == "EXT_URL" and forum.lever_class == "external_community"
    assert forum.evidence["activity"]["posts"] == 240 and forum.evidence["activity"]["members"] == 1200
    assert len(forum.evidence["shared_tokens"]) >= 2 and "night" in forum.evidence["shared_tokens"]
    assert "nicht gelesen" in forum.evidence["uncertainty"]
    assert forum.http_status == 200 and forum.access["actionable"] is True


def test_a_dead_page_is_dropped_and_unknown_activity_stays_unknown(session, configured):
    theme(session)
    client = FakeSearch([("Night train ambient community", "https://tot.example/forum/1",
                          "night train ambient journey")], page_status=410)
    aq.collect(session, NOW, http=client)
    assert not list(session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "web_community")))
    assert aq.activity_signals("nur Text ohne Zahlen")["known"] is False
    assert "unbekannt" in aq.activity_signals("nur Text")["note"]


def test_a_found_pool_becomes_an_executable_proposal_with_external_attribution(session, configured):
    theme(session)
    client = FakeSearch([("Night train ambient - Forum", "https://bahnforum.example/viewtopic.php?t=91",
                          "night train ambient journey Diskussion, 3.4k members")])
    aq.collect(session, NOW, http=client)
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.traffic_source == "EXT_URL" and row.target_metric == "external_views_7d"
    payload = row.payload
    assert payload["surface_url"].startswith("https://bahnforum.example")
    assert "night" in payload["why"] and payload["evidence"]["activity"]["members"] == 3400
    assert any("Seite öffnen und lesen" in step for step in payload["steps"])
    assert any("nur setzen, wenn die Regeln" in step for step in payload["steps"])
    assert payload["mechanism_status"] == "hypothese"
    assert "Test dieser Hypothese" in payload["mechanism_note"]
    assert "attribuierte Views" in payload["upgrade_rule"]
    assert "EXT_URL" in payload["primary_metric"]


def test_an_unproven_lever_is_capped_and_only_real_results_lift_it(session, configured):
    theme(session)
    client = FakeSearch([("Night train ambient - Forum", "https://bahnforum.example/viewtopic.php?t=91",
                          "night train ambient journey, 900k members, 500k posts")])
    aq.collect(session, NOW, http=client)
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "web_community"))
    unproven_score = surface.scores["traffic_potential"]
    assert unproven_score <= aq.HYPOTHESIS_CAP
    assert surface.scores["mechanism"] == "hypothese" and "hypothese" in surface.scores["adjusted_by"]
    levers = aq.lever_record(session)
    assert levers["external_community"]["status"] == "hypothese" and levers["external_community"]["proven"] is False
    assert "unbelegt" in levers["external_community"]["basis"]
    # Drei ausgewertete Versuche mit echtem Zufluss werten den Hebel auf.
    for i in range(3):
        session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=40+i), created_at=NOW,
                                 version=aq.VERSION, state="traffic_acquisition",
                                 action="participate_in_web_community", target_metric="external_views_7d",
                                 window_days=14, evaluate_after=TODAY-timedelta(days=20), status="evaluated",
                                 outcome="positive", lever_class="external_community", traffic_source="EXT_URL",
                                 surface_key=f"k{i}", payload={"surface_kind": "web_community"}, baseline={},
                                 evaluation={"attribution": {"views_delta": 12}}))
    session.commit()
    proven = aq.lever_record(session)["external_community"]
    assert proven["proven"] is True and proven["status"] == "belegt" and proven["weight"] > 1.0
    assert aq.mechanism_status(surface, aq.lever_record(session))["status"] == "belegt"
    # Dieselbe Flaeche wird nach belegten Ergebnissen hoeher bewertet – nur deshalb.
    aq.collect(session, NOW+timedelta(days=1), http=client)
    after = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "web_community",
                                                       TrafficSurface.day == ge.pacific_day(NOW+timedelta(days=1))))
    assert after.scores["traffic_potential"] > unproven_score and after.scores["mechanism"] == "belegt"


def test_external_acquisition_is_not_blocked_by_the_running_impressions_experiment(session, configured):
    theme(session)
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence",
                             target_metric="impressions_7d", window_days=14, evaluate_after=TODAY+timedelta(days=16),
                             status="running", started_day=TODAY-timedelta(days=1), started_at=NOW,
                             lever_class="internal_link", traffic_source="END_SCREEN", payload={}))
    session.commit()
    client = FakeSearch([("Night train ambient - Forum", "https://bahnforum.example/viewtopic.php?t=91",
                          "night train ambient journey Diskussion, 800 members")])
    aq.collect(session, NOW, http=client)
    assert aq.propose(session, NOW)["proposed"] == 1, "eine externe Quelle ist von Impressionen trennbar"
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.video_id == "a" and row.traffic_source == "EXT_URL"
    # Eine Empfehlungsflaeche bleibt gesperrt: eine Empfehlung IST eine Impression.
    other, reason = aq.blocking(session, "a", "community_participation", "RELATED_VIDEO", "suggested_views_7d")
    assert other is not None and "impressions_7d" in reason
    # Ein Kommentar auf einer fremden Videoseite erzeugt keine Impression und ist trennbar.
    free, _ = aq.blocking(session, "a", "community_participation", "YT_OTHER_PAGE", "other_views_7d")
    assert free is None


def test_the_manual_trigger_runs_a_pass_and_reports_the_search_state(monkeypatch, session, configured):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    theme(session)
    main.app.dependency_overrides[main.db] = lambda: session
    try:
        client = TestClient(main.app)
        assert client.post("/api/acquisition/run").status_code in (401, 403)
        body = client.post("/api/acquisition/run", headers=HEADERS).json()
        assert body["version"] == aq.VERSION and "web_search" in body
        view = client.get("/api/acquisition", headers=HEADERS).json()
        assert view["web_search"]["configured"] is True
        assert view["web_search"]["daily_limit"] == 50 and view["levers"]
    finally:
        main.app.dependency_overrides.clear()
