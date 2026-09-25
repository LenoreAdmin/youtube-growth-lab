"""Audience-Discovery mit dem vorhandenen Stack: fremde Playlists, Kanäle und eigene Einbettungen.

Kein zusätzlicher Anbieter, keine neue Infrastruktur: alles über die bereits autorisierten YouTube-APIs
innerhalb des bestehenden Quota-Budgets. Geprüft wird vor allem, dass nur wirklich passende und
nachprüfbare Orte durchkommen – und dass die Attribution je Quelle stimmt.
"""
from datetime import date, timedelta
import pytest
from sqlalchemy import select
from app import acquisition as aq, discovery, growth_engine as ge
from app.models import (AudiencePool, DiscoveryItem, DiscoveryQuery, DiscoverySignal, GrowthAction, TrafficSurface,
                        Video, VideoProfile)
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG
from test_acquisition import FakeHttp, signal

WINDOW_END = TODAY-timedelta(days=LAG)


class PoolClient:
    """Liefert genau die öffentlichen Daten, die die echten API-Aufrufe liefern würden."""

    def __init__(self, playlists=(), channels=(), items=None, owners=None, embeds=()):
        self.playlists, self.channels = list(playlists), list(channels)
        self.items, self.owners, self.embeds = items or {}, owners or {}, list(embeds)
        self.calls = []

    def search_playlists(self, query, max_results=25):
        self.calls.append(("search_playlists", query))
        return self.playlists

    def search_channels(self, query, max_results=25):
        self.calls.append(("search_channels", query))
        return self.channels

    def playlists_by_id(self, ids):
        self.calls.append(("playlists_by_id", tuple(ids)))
        return [{"id": p["playlist_id"],
                 "snippet": {"title": p["title"], "channelId": p["channel_id"], "channelTitle": p["channel_title"],
                             "description": p.get("description", "")},
                 "contentDetails": {"itemCount": p.get("item_count", 12)},
                 "status": {"privacyStatus": "public"}} for p in self.playlists if p["playlist_id"] in set(ids)]

    def playlist_items(self, playlist_id, max_results=10):
        self.calls.append(("playlist_items", playlist_id))
        return [{"snippet": {"title": title}} for title in self.items.get(playlist_id, [])]

    def channels_by_id(self, ids, part=None):
        self.calls.append(("channels_by_id", tuple(ids)))
        rows = []
        for channel_id in ids:
            data = self.owners.get(channel_id)
            if data is None:
                continue
            rows.append({"id": channel_id,
                         "snippet": {"title": data.get("title", channel_id), "description": data.get("description", ""),
                                     "country": data.get("country")},
                         "statistics": {"subscriberCount": str(data["subscribers"]), "videoCount": str(data.get("videos", 50)),
                                        "viewCount": str(data.get("views", 1000))},
                         "topicDetails": {"topicCategories": data.get("topics", [])},
                         "brandingSettings": {"channel": {"keywords": data.get("keywords", "")}}})
        return rows

    def embedded_locations(self, video, start, end, max_results=25):
        self.calls.append(("embedded_locations", video))
        return [{"insightPlaybackLocationDetail": url, "views": views, "estimatedMinutesWatched": views*2.0}
                for url, views in self.embeds]


def seed_profile(session, video_id="a", tags=("night train ambient", "trans mongolian"),
                 description="Ein Ambient-Stück über eine Reise mit dem Nachtzug quer durch die Steppe.",
                 topics=("https://en.wikipedia.org/wiki/Ambient_music",),
                 channel_description="Ambient und Field Recordings", channel_keywords="ambient journey"):
    """Die eigenen öffentlichen Metadaten – ohne sie besteht unser Thema aus einem Titelwort."""
    session.add(VideoProfile(video_id=video_id, description=description, tags=list(tags), topics=list(topics),
                            category_id="10", channel_title="Sealand", channel_description=channel_description,
                            channel_keywords=channel_keywords, channel_topics=list(topics), fetched_day=TODAY))
    session.commit()


def seed_theme(session):
    """Ein Video mit belegter Nachbarschaft und eigenen Metadaten, damit ein Thema überhaupt existiert."""
    session.get(Video, "a").title = "Sealand Trainstories"
    seed_profile(session)
    session.add(DiscoveryItem(video_id="NEIGHBOUR01", channel_id="UCN", title="Night train ambient journey",
                              channel_title="Rail Nights", views=120000, tags=[],
                              via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY, last_seen_day=TODAY,
                              seen_count=1))
    session.commit()
    signal(session, "a", "own_suggested_source", "NEIGHBOUR01", 4)


# ---------------------------------------------------------------------------- Probe im Quota-Rahmen
def test_the_probe_finds_foreign_playlists_and_channels_within_the_existing_quota(session):
    seed_theme(session)
    client = PoolClient(
        playlists=[{"playlist_id": "PL1", "title": "Night Train Ambient Journeys", "channel_id": "UCcur",
                    "channel_title": "Slow Travel Sounds", "published_at": "2026-01-01T00:00:00Z",
                    "description": "ambient night train journey playlist", "item_count": 40}],
        channels=[{"channel_id": "UCpool", "title": "Night Train Radio", "description": "ambient night train journey",
                   "published_at": "2024-01-01T00:00:00Z"}],
        items={"PL1": ["Night train ambient journey", "Rails at midnight"]},
        owners={"UCcur": {"title": "Slow Travel Sounds", "subscribers": 82000, "views": 9000000},
                "UCpool": {"title": "Night Train Radio", "subscribers": 45000, "views": 5000000,
                           "keywords": "night train ambient journey", "topics": ["https://en.wikipedia.org/wiki/Ambient_music"]}})
    quota = discovery.Quota(session, TODAY, limit=1500)
    stats = {}
    discovery.probe_audience_pools(session, client, quota, list(session.scalars(select(Video))), NOW, None, stats)
    pools = {(p.kind, p.key): p for p in session.scalars(select(AudiencePool))}
    assert set(pools) == {("playlist", "PL1"), ("channel", "UCpool")}
    playlist = pools[("playlist", "PL1")]
    assert playlist.item_count == 40 and playlist.subscribers == 82000
    assert playlist.url == "https://www.youtube.com/playlist?list=PL1"
    assert playlist.details["items"][:1] == ["Night train ambient journey"]
    channel = pools[("channel", "UCpool")]
    assert channel.subscribers == 45000 and "Ambient_music" in channel.details["topics"][0]
    # Zwei Suchen zu je 100 Einheiten plus wenige Detailabfragen – weit unter dem Budget.
    assert quota.spent_now <= 2*discovery.SEARCH_COST+5*discovery.LIST_COST
    assert stats["pools"] == 2
    assert sum(1 for call in client.calls if call[0].startswith("search")) == 2
    # Beide Suchen laufen und sind einzeln nachpruefbar: Query, Herkunft, Trefferzahl, Titel, Gespeichertes.
    report = stats["pool_probe"]
    assert [q["kind"] for q in report["queries"]] == ["playlist", "channel"]
    # Die Queries kommen aus Audience-Intents, nicht aus einzelnen Tags.
    assert all(q["source"] in ("topic_context", "artist_adjacency", "search_demand") for q in report["queries"])
    assert all(len(q["query"].split()) >= 2 and q["intent"] for q in report["queries"])
    assert pools[("playlist", "PL1")].details["intent"]["head"], "der Intent haengt am Fund"
    assert report["queries"][0]["titles"] == ["Night Train Ambient Journeys"]
    assert report["queries"][0]["results"] == 1 and report["queries"][0]["stored"] == 1
    assert report["candidates"] == 2 and report["stored"] == 2 and report["note"] is None


def test_the_probe_never_searches_for_format_words_and_says_so_when_it_cannot(session):
    """Der BLACKPINK-Fehlgriff kam von „album teaser“: Formatwoerter und Release-Namen sind keine Themen."""
    session.get(Video, "a").title = "11AM Album - Teaser"
    session.get(Video, "b").title = "Shine On"
    session.commit()
    seed_profile(session, tags=("11am", "album", "teaser", "official video"), description="11AM Album Teaser",
                 topics=(), channel_description="", channel_keywords="")
    client = PoolClient(playlists=[], channels=[])
    stats = {}
    quota = discovery.Quota(session, TODAY, limit=1500)
    discovery.probe_audience_pools(session, client, quota, list(session.scalars(select(Video))), NOW, None, stats)
    assert client.calls == [] and quota.spent_now == 0
    assert stats["pool_probe"]["queries"] == []
    assert "Audience-Intent" in stats["pool_probe"]["note"]


def test_a_single_usable_theme_query_still_searches_playlists_and_channels(session):
    """Fehler der ersten Fassung: mit nur einem Intent lief die Kanalsuche nie."""
    session.get(Video, "a").title = "Sealand Trainstories"
    seed_profile(session, tags=("night train ambient",), description="Nachtzug, Ambient.", topics=())
    client = PoolClient(playlists=[], channels=[])
    stats = {}
    quota = discovery.Quota(session, TODAY, limit=1500)
    discovery.probe_audience_pools(session, client, quota, list(session.scalars(select(Video))), NOW, None, stats)
    kinds = [call[0] for call in client.calls]
    assert kinds == ["search_playlists", "search_channels"]
    assert [q["note"] for q in stats["pool_probe"]["queries"]] == ["Keine fremden Playlists in den Treffern.",
                                                                  "Keine fremden Kanaele in den Treffern."]


def test_the_probe_stops_before_spending_a_budget_it_does_not_have(session):
    seed_theme(session)
    client = PoolClient(playlists=[{"playlist_id": "PL1", "title": "x", "channel_id": "UCc", "channel_title": "c",
                                    "published_at": "2026-01-01T00:00:00Z"}])
    quota = discovery.Quota(session, TODAY, limit=50)      # weniger als eine Suche kostet
    stats = {}
    discovery.probe_audience_pools(session, client, quota, list(session.scalars(select(Video))), NOW, None, stats)
    assert not list(session.scalars(select(AudiencePool))) and client.calls == []
    assert all("Tagesbudget erschoepft" in q["note"] for q in stats["pool_probe"]["queries"])


def test_own_embeds_are_collected_from_analytics_without_data_api_quota(session):
    client = PoolClient(embeds=[("bahnblog.example/nachtzug", 9), ("", 3)])
    stats = {}
    discovery.collect_embeds(session, client, list(session.scalars(select(Video))), NOW, None, stats)
    rows = list(session.scalars(select(DiscoverySignal).where(DiscoverySignal.kind == "own_embed")))
    assert [r.detail for r in rows if r.video_id == "a"] == ["bahnblog.example/nachtzug"]
    assert rows[0].views == 9 and stats["embeds"] >= 1


# ---------------------------------------------------------------------------- Relevanz der Pools
def pool(session, kind, key, title, **fields):
    fields["details"] = {"contains_own": False, **(fields.get("details") or {})}
    session.add(AudiencePool(**{**{"kind": kind, "key": key, "title": title,
                                   "url": f"https://www.youtube.com/playlist?list={key}" if kind == "playlist"
                                          else f"https://www.youtube.com/channel/{key}",
                                   "first_seen_day": TODAY, "last_seen_day": TODAY, "details": {}}, **fields}))
    session.commit()


def test_a_playlist_needs_a_real_theme_or_a_proven_neighbourhood(session):
    seed_theme(session)
    pool(session, "playlist", "PLgood", "Night train ambient journeys", item_count=30, subscribers=50000,
         channel_title="Slow Travel", description="ambient night train journey")
    pool(session, "playlist", "PLgeneric", "Best Album Teaser Playlist", item_count=30, subscribers=900000,
         channel_title="Mainstream", description="album teaser single visual")
    pool(session, "playlist", "PLtiny", "Night train ambient", item_count=2, subscribers=50000,
         channel_title="Entwurf", description="ambient night train journey")
    pool(session, "playlist", "PLneighbour", "Zugfahrten", item_count=12, subscribers=3000,
         channel_title="Kurator", description="Sammlung",
         details={"items": ["Night train ambient journey", "Etwas anderes"]})
    aq.collect(session, NOW, http=FakeHttp())
    kept = {r.key for r in session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "curated_playlist"))}
    assert "PLgood" in kept, "spezifische gemeinsame Begriffe genuegen"
    assert "PLneighbour" in kept, "ein Video aus der belegten Nachbarschaft genuegt ebenfalls"
    assert "PLgeneric" not in kept, "Formatwoerter sind kein Thema"
    assert "PLtiny" not in kept, "zwei Titel sind kein gepflegter Ort"
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.key == "PLneighbour"))
    assert surface.evidence["neighbourhood_overlap"] == ["Night train ambient journey"]
    assert surface.evidence["audience_fit"]["class"] == "neighbourhood"
    assert "belegte Nachbarschaft" in surface.evidence["why"]
    assert surface.traffic_source == "PLAYLIST" and surface.lever_class == "playlist_placement"


def test_a_found_channel_needs_theme_and_real_size(session):
    seed_theme(session)
    pool(session, "channel", "UCbig", "Night Train Radio", subscribers=45000, item_count=300, views=5000000,
         description="ambient night train journey radio",
         details={"keywords": "night train ambient", "topics": ["https://en.wikipedia.org/wiki/Ambient_music"]})
    pool(session, "channel", "UCsmall", "Night Train Tiny", subscribers=12, item_count=3,
         description="ambient night train journey")
    pool(session, "channel", "UCoff", "Kochkanal", subscribers=900000, item_count=800, description="Rezepte und Kuchen")
    aq.collect(session, NOW, http=FakeHttp())
    kept = {r.key for r in session.scalars(select(TrafficSurface).where(TrafficSurface.kind == "pool_channel"))}
    assert kept == {"UCbig"}
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "pool_channel"))
    assert surface.evidence["subscribers"] == 45000 and surface.traffic_source == "YT_OTHER_PAGE"
    assert "kennt uns nicht" in surface.evidence["why"]


def test_every_rejected_candidate_names_its_concrete_reason(session):
    """Ein leerer Trichter ohne Begruendung ist nicht verbesserbar: jeder Kandidat braucht ein Verdikt."""
    seed_theme(session)
    pool(session, "playlist", "PLgood", "Night train ambient journeys", item_count=30, subscribers=50000,
         channel_title="Slow Travel", description="ambient night train journey",
         query="night train ambient", details={"query_source": "own_tag"})
    pool(session, "playlist", "PLtiny", "Night train ambient", item_count=2, subscribers=50000,
         channel_title="Entwurf", description="ambient night train journey", query="night train ambient")
    pool(session, "channel", "UCsmall", "Night Train Tiny", subscribers=12, item_count=3,
         description="ambient night train journey", query="night train ambient")
    pool(session, "channel", "UCoff", "Kochkanal", subscribers=900000, item_count=800, description="Rezepte",
         query="night train ambient")
    result = aq.collect(session, NOW, http=FakeHttp())
    verdicts = {v["title"]: v for v in result["pools"]["verdicts"]}
    assert result["pools"]["candidates"] == 4 and result["pools"]["kept"] == 1
    assert verdicts["Night train ambient journeys"]["kept"] is True
    assert "Titel" in verdicts["Night train ambient"]["reason"] and "mindestens 5" in verdicts["Night train ambient"]["reason"]
    assert "Abonnenten" in verdicts["Night Train Tiny"]["reason"]
    assert "Keine belegbare Audience-Naehe" in verdicts["Kochkanal"]["reason"]
    assert verdicts["Kochkanal"]["fit_class"] is None
    assert verdicts["Night train ambient journeys"]["size"] == "30 Titel"
    assert verdicts["Night Train Tiny"]["size"] == "12 Abonnenten"
    # Das Verdikt haengt am Kandidaten, damit das Dashboard es ohne neuen Lauf zeigen kann.
    stored = {p.key: (p.details or {}).get("verdict") for p in session.scalars(select(AudiencePool))}
    assert stored["PLtiny"]["kept"] is False and stored["PLgood"]["kept"] is True
    view = aq.overview(session, NOW)
    report = view["pools"]
    assert report["kept"] == 1 and report["rejected"] == 3
    shown = {c["title"]: c for c in report["candidates"]}
    assert shown["Night train ambient journeys"]["query"] == "night train ambient"
    assert shown["Night train ambient journeys"]["query_source"] == "own_tag"
    assert shown["Night Train Tiny"]["reason"]


def test_the_search_probes_leave_units_for_the_pool_search(session):
    """Vorher verbrauchten die acht Suchproben das Budget, und die Pool-Suche lief still ins Leere."""
    for index in range(8):
        session.add(DiscoveryQuery(query=f"thema {index}", source="title_bigram", seed_video_id="a",
                                   priority=500.0-index, created_at=NOW, probe_count=0, failures=0, results={}))
    session.commit()

    class SearchClient:
        def __init__(self):
            self.searches = 0

        def search(self, query, max_results=25):
            self.searches += 1
            return []

        def videos_by_id(self, ids, part=None):
            return []

    client = SearchClient()
    quota = discovery.Quota(session, TODAY, limit=discovery.DAILY_UNITS)
    quota.spend(900)        # wie nach einem frueheren Lauf am selben Pacific-Tag
    stats = {"searches": 0}
    discovery.probe_queries(session, client, quota, list(session.scalars(select(Video))), NOW, None, stats)
    assert stats["pool_reserve_kept"] == discovery.POOL_RESERVE
    assert quota.remaining() >= 2*discovery.SEARCH_COST, "zwei Pool-Suchen bleiben bezahlbar"
    assert client.searches < 8


def test_an_embedding_site_is_measured_but_never_contacted(session):
    """PRODUKTREGEL: wer unser Video schon einbettet, hat uns selbst ausgewaehlt. Das ist eine Beziehung."""
    signal(session, "a", "own_embed", "bahnblog.example/nachtzug", 12)
    aq.collect(session, NOW, http=FakeHttp())
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "embed_site"))
    assert surface.traffic_source == "EXT_URL" and surface.http_status == 200
    assert "bettet unser Video ein" in surface.evidence["why"] and surface.evidence["measured_views_90d"] == 12
    assert surface.access["actionable"] is False and surface.access["protected"] is True
    assert surface.access["protected_reason"] == "protected_existing_source"
    assert aq.propose(session, NOW)["proposed"] == 0


# ---------------------------------------------------------------------------- Attribution und Konflikte
def test_each_new_surface_is_attributed_to_its_own_source(session):
    assert aq.SURFACE_KINDS["curated_playlist"]["metric"] == "playlist_views_7d"
    assert aq.METRIC_SOURCES["playlist_views_7d"] == {"PLAYLIST", "YT_PLAYLIST_PAGE"}
    assert aq.SURFACE_KINDS["embed_site"]["metric"] == "external_views_7d"
    assert aq.SURFACE_KINDS["pool_channel"]["metric"] == "other_views_7d"


def test_the_running_impressions_test_blocks_only_the_youtube_surfaces(session):
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence",
                             target_metric="impressions_7d", window_days=14, evaluate_after=TODAY+timedelta(days=16),
                             status="running", started_day=TODAY-timedelta(days=1), started_at=NOW,
                             lever_class="internal_link", traffic_source="END_SCREEN", payload={}))
    session.commit()
    # Eine Playlist-Platzierung erzeugt Impressionen auf der Playlist-Seite: nicht trennbar.
    blocked, reason = aq.blocking(session, "a", "playlist_placement", "PLAYLIST", "playlist_views_7d")
    assert blocked is not None and "impressions_7d" in reason
    # Eine fremde Seite und ein Kommentar auf einer Videoseite erzeugen keine Impression: trennbar.
    assert aq.blocking(session, "a", "external_outreach", "EXT_URL", "external_views_7d")[0] is None
    assert aq.blocking(session, "a", "community_participation", "YT_OTHER_PAGE", "other_views_7d")[0] is None


def test_a_playlist_pitch_becomes_an_executable_proposal(session):
    # Nachbarschaft nur als Themenquelle, ohne eigene Reichweite – damit die Playlist die beste Flaeche ist.
    session.get(Video, "a").title = "Sealand Trainstories"
    session.add(DiscoveryItem(video_id="NEIGHBOUR01", channel_id="UCN", title="Night train ambient journey",
                              channel_title="Rail Nights", views=400, tags=[],
                              via={"suggested_source": ["own_traffic"]}, first_seen_day=TODAY, last_seen_day=TODAY,
                              seen_count=1))
    session.commit()
    signal(session, "a", "own_suggested_source", "NEIGHBOUR01", 4)
    pool(session, "playlist", "PLgood", "Night train ambient journeys", item_count=30, subscribers=50000,
         channel_title="Slow Travel Sounds", description="ambient night train journey")
    aq.collect(session, NOW, http=FakeHttp())
    assert aq.propose(session, NOW)["proposed"] == 1
    row = session.scalar(select(GrowthAction).where(GrowthAction.version == aq.VERSION))
    assert row.action == "pitch_to_playlist_curator" and row.target_metric == "playlist_views_7d"
    payload = row.payload
    assert "Slow Travel Sounds" in payload["why"] and "30 Titel" in payload["why"]
    assert any("Kurator" in step for step in payload["steps"])
    assert any("keine Gegenleistung" in step for step in payload["steps"])
    assert payload["mechanism_status"] == "hypothese"
    assert "PLAYLIST" in payload["primary_metric"]
    entry = aq.overview(session, NOW)["traffic_queue"][0]
    assert entry["surface_url"] == "https://www.youtube.com/playlist?list=PLgood"
    assert entry["traffic_source"] == "PLAYLIST"


def test_playlist_outreach_picks_the_full_music_video_not_the_album_teaser(session):
    """Produktionsfall: die Swiss-Pop-Playlists bekamen automatisch den 11AM-Album-Teaser angeboten."""
    from app.models import Video as V
    session.get(Video, "a").title = "Sealand Trainstories"
    session.get(Video, "a").duration_seconds = 214
    session.add(V(id="c", channel_id=session.get(Video, "a").channel_id, title="11AM Album - Teaser",
                  published_at=session.get(Video, "a").published_at, duration_seconds=42, active=True))
    session.commit()
    seed_profile(session, tags=["swiss pop", "pop", "ambient"], description="Schweizer Pop aus dem Nachtzug.",
                 topics=("https://en.wikipedia.org/wiki/Pop_music",))
    seed_profile(session, video_id="c", tags=["swiss pop", "pop"], description="11AM Album Teaser", topics=(),
                 channel_description="", channel_keywords="")
    pool(session, "playlist", "PLswiss", "Swiss Pop Music", item_count=120, subscribers=8000,
         channel_title="Swiss Charts", description="Die besten Songs aus der Schweiz: swiss pop",
         query="switzerland pop", details={"items": ["Ein Schweizer Pop Song", "Noch ein Pop Titel"]})
    result = aq.collect(session, NOW, http=FakeHttp())
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.kind == "curated_playlist"))
    assert surface is not None, [v["reason"] for v in result["pools"]["verdicts"]]
    assert surface.video_id == "a", "ein 42-Sekunden-Teaser gehoert in keine Playlist-Anfrage"
    assert "Trainstories" in surface.evidence["why"]
    assert surface.evidence["audience_fit"]["class"] in ("genre", "topic", "neighbourhood")


def test_a_channel_needs_genre_or_neighbourhood_evidence_not_only_english_words(session):
    """YT_OTHER_PAGE ist eine Kommentar-Flaeche: dort zaehlt musikalische Naehe oder belegte Nachbarschaft."""
    seed_theme(session)
    pool(session, "channel", "UCrandom", "Long Ride Trucking", subscribers=40000, item_count=200,
         description="long rides out on the road, good times", query="night train ambient")
    pool(session, "channel", "UCmusic", "Night Ambient Radio", subscribers=40000, item_count=200,
         description="ambient night train music radio", query="night train ambient",
         details={"topics": ["https://en.wikipedia.org/wiki/Ambient_music"], "keywords": "ambient night"})
    result = aq.collect(session, NOW, http=FakeHttp())
    verdicts = {v["title"]: v for v in result["pools"]["verdicts"]}
    assert verdicts["Long Ride Trucking"]["kept"] is False
    assert verdicts["Long Ride Trucking"]["fit_class"] is None
    assert verdicts["Night Ambient Radio"]["kept"] is True
    assert verdicts["Night Ambient Radio"]["fit_class"] in ("genre", "neighbourhood")


def test_the_engine_says_plainly_when_no_action_is_good_enough(session):
    """Ein leeres JETZT TUN ist eine Aussage, kein Fehler – und muss als solche dastehen."""
    seed_theme(session)
    pool(session, "channel", "UCnothing", "Kochkanal", subscribers=900000, item_count=800, description="Rezepte",
         query="night train ambient")
    aq.collect(session, NOW, http=FakeHttp())
    view = aq.overview(session, NOW)
    assert view["traffic_queue"] == []
    assert view["assessment"]["status"] == "none" and view["assessment"]["actions"] == 0
    assert "keine ausreichend gute Traffic-Aktion" in view["assessment"]["text"]


def test_a_topic_only_fit_is_declared_a_hypothesis_and_capped(session):
    """Produktionsfall: 1,13 Mio. Abonnenten auf einem Reisekanal sind kein Musikpublikum."""
    seed_theme(session)
    pool(session, "channel", "UCtravel", "Travel With Koushik", subscribers=1130000, item_count=500,
         views=200000000, description="train journey across mongolian railway and the steppe",
         query="trans mongolian",
         details={"contains_own": False, "keywords": "travel train journey",
                  "intent": {"head": ["trans", "mongolian"], "context": [], "label": "„trans mongolian“",
                             "attestations": 2, "video_id": "a", "kind": "topic_context"}})
    aq.collect(session, NOW, http=FakeHttp())
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.key == "UCtravel"))
    assert surface.evidence["audience_fit"]["class"] == "topic"
    assert surface.scores["traffic_potential"] <= aq.TOPIC_ONLY_CAP, "Reichweite ersetzt keine Evidenz"
    aq.propose(session, NOW)
    view = aq.overview(session, NOW)
    assert view["assessment"]["status"] == "hypothesis_only"
    assert "Hypothese" in view["assessment"]["text"]


def test_a_blocked_video_does_not_waste_a_playlist_that_fits_another_one(session):
    """Massnahme #8 sperrt Trainstories fuer PLAYLIST – Shine On ist frei und passt ebenfalls."""
    from app.models import GrowthAction
    from app import growth_engine as ge
    session.get(Video, "a").title = "Sealand Trainstories"
    session.get(Video, "a").duration_seconds = 214
    session.get(Video, "b").title = "Sealand Shine On"
    session.get(Video, "b").duration_seconds = 248
    session.commit()
    seed_profile(session, tags=("swiss pop", "ambient"), description="Schweizer Pop aus dem Nachtzug.",
                 topics=("https://en.wikipedia.org/wiki/Pop_music",))
    seed_profile(session, video_id="b", tags=("swiss pop", "acoustic"), description="Schweizer Pop, akustisch.",
                 topics=("https://en.wikipedia.org/wiki/Pop_music",), channel_description="", channel_keywords="")
    session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence",
                             target_metric="impressions_7d", window_days=14,
                             evaluate_after=TODAY+timedelta(days=16), status="running",
                             started_day=TODAY-timedelta(days=1), started_at=NOW, lever_class="internal_link",
                             traffic_source="END_SCREEN", payload={}))
    session.commit()
    pool(session, "playlist", "PLswisspop", "Swiss Pop Music", item_count=120, subscribers=8000,
         channel_title="Swiss Charts", description="swiss pop hits aus der Schweiz", query="switzerland pop",
         details={"items": ["Schweizer Pop Song", "Pop aus Zuerich"]})
    aq.collect(session, NOW, http=FakeHttp())
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.key == "PLswisspop"))
    assert surface is not None and surface.video_id == "b", "die freie, passende Veroeffentlichung nimmt den Platz"
    assert aq.propose(session, NOW)["proposed"] == 1
    entry = aq.overview(session, NOW)["traffic_queue"][0]
    assert entry["traffic_source"] == "PLAYLIST" and entry["title"] == "Sealand Shine On"
