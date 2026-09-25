"""Audience-Intents: aus welchen eigenen Daten ein Thema entsteht – und was ausdrücklich keines ist.

Die Produktionsläufe haben genau zwei Fehler gezeigt, und beide werden hier festgehalten:
„11am album teaser“ durfte nie ein Audience-Thema sein, und „Mongolian Trains“ durfte nicht daran
scheitern, dass es mit „Sealand Trainstories“ kein Wort teilt.
"""
from datetime import timedelta
import pytest
from sqlalchemy import select
from app import acquisition as aq, audience
from app.models import AudiencePool, DiscoveryItem, DiscoverySignal, TrafficSurface, Video, VideoProfile
from test_learning_v4 import seed_history, wire, NOW, TODAY
from test_acquisition import FakeHttp, signal
from test_pools import PoolClient, pool


@pytest.fixture()
def session(session):      # noqa: F811 - dieselbe Sitzung wie in den anderen Suites
    return session


def profile(session, **fields):
    defaults = {"video_id": "a", "description": "", "tags": [], "topics": [], "category_id": "10",
                "channel_title": "Sealand", "channel_description": "", "channel_keywords": "",
                "channel_topics": [], "fetched_day": TODAY}
    session.add(VideoProfile(**{**defaults, **fields}))
    session.commit()


def neighbour(session, video_id, title, channel_title, views, tags=()):
    session.add(DiscoveryItem(video_id=video_id, channel_id="UC"+video_id, title=title, channel_title=channel_title,
                              views=200000, tags=list(tags), via={"suggested_source": ["own_traffic"]},
                              first_seen_day=TODAY, last_seen_day=TODAY, seen_count=1))
    session.commit()
    signal(session, "a", "own_suggested_source", video_id, views)


# ---------------------------------------------------------------------------- Kategorien
def test_release_and_format_words_never_become_an_audience_topic(session):
    """„11AM Album - Teaser“: Release-Name, Formatwörter, Marke. Kein Wort davon ist ein Interesse."""
    session.get(Video, "a").title = "11AM Album - Teaser"
    session.commit()
    profile(session, tags=["11am", "album", "teaser", "official video", "sealand"], description="11AM Album Teaser")
    rows = {row["term"]: row["category"] for row in audience.profile_terms(session, session.get(Video, "a"))}
    assert rows["11am"] == "release", "eine Zeitmarke ist keine Zielgruppe"
    # Beides sind ausgeschlossene Kategorien: „album“ steht nur im eigenen Titel, „teaser“ ist ein Formatwort.
    assert rows["album"] in ("format", "release") and rows["teaser"] in ("format", "release")
    assert "sealand" not in rows, "der Markenname wird bereits beim Zerlegen entfernt"
    assert not audience.build_intents(session, session.get(Video, "a")), "daraus entsteht kein Intent"


def test_a_title_only_word_is_treated_as_the_release_name(session):
    """„Trainstories“ steht nur im eigenen Titel: niemand sucht danach, es benennt diese Veröffentlichung."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["night train ambient"], description="Nachtzug durch die Steppe.")
    rows = {row["term"]: row for row in audience.profile_terms(session, session.get(Video, "a"))}
    assert rows["trainstories"]["category"] == "release"
    assert rows["train"]["category"] == "ort" and rows["ambient"]["category"] == "genre"


def test_categories_come_from_our_own_data_not_from_invention(session):
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "relaxing"], description="Eine Reise mit der Transmongolischen Bahn.",
            topics=["https://en.wikipedia.org/wiki/Ambient_music"])
    rows = {row["term"]: row for row in audience.profile_terms(session, session.get(Video, "a"))}
    assert rows["mongolian"]["category"] == "ort" and rows["relaxing"]["category"] == "mood"
    assert rows["ambient"]["category"] == "genre" and "own_topic" in rows["ambient"]["sources"]
    assert rows["music"]["sources"] == ["own_topic"], "YouTube führt das Video selbst als Musik"
    # Jeder Begriff nennt seine Quelle im Klartext.
    labels = [item["label"] for item in rows["mongolian"]["evidence"]]
    assert any("Tag" in label for label in labels)


# ---------------------------------------------------------------------------- Intents und Belege
def test_an_intent_needs_a_head_a_context_and_two_independent_sources(session):
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "ambient"], description="Ambient zur Reise mit der Bahn nach Mongolian.",
            topics=["https://en.wikipedia.org/wiki/Ambient_music"])
    intents = audience.build_intents(session, session.get(Video, "a"))
    assert intents, "ein belegtes Thema mit Kontext muss einen Intent ergeben"
    first = intents[0]
    assert first["attestations"] >= audience.MIN_ATTESTATIONS
    assert "mongolian" in first["head"] and first["context"]
    assert len(first["query"].split()) >= 2 and first["kind"] == "topic_context"
    sources = {item["source"] for item in first["evidence"]}
    assert len(sources) >= 2 and "own_tag" in sources


def test_a_proven_neighbour_artist_becomes_its_own_intent(session):
    """Ein Kanal, neben dem YouTube uns messbar ausliefert, ist eine belegte Audience."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["night train ambient"])
    neighbour(session, "NB000000001", "Night train ambient journey", "Rail Nights", 6)
    intents = audience.build_intents(session, session.get(Video, "a"))
    artist = next(i for i in intents if i["kind"] == "artist_adjacency")
    assert artist["head"] == ["rail", "nights"] and artist["views"] >= 6
    assert any(item["source"] == "neighbour_artist" for item in artist["evidence"])


def test_an_intent_that_only_ever_produced_rejects_is_retired(session):
    """Lernen an echten Ergebnissen: ein Query mit vielen Kandidaten und null Treffern wird zurueckgestellt."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "ambient"], description="Ambient zur Reise nach Mongolian.")
    live = audience.build_intents(session, session.get(Video, "a"))
    query = next(i["query"] for i in live if "mongolian" in i["head"])
    for index in range(audience.RETIRE_AFTER_CANDIDATES+1):
        pool(session, "playlist", f"PLjunk{index}", f"Good Times Full Episodes {index}", item_count=30, query=query,
             details={"verdict": {"kept": False, "reason": "kein Bezug"}})
    after = audience.build_intents(session, session.get(Video, "a"))
    assert query not in [i["query"] for i in after], "ein toter Intent verbraucht keine Quota mehr"


# ---------------------------------------------------------------------------- der Matching-Bug
def test_query_evidence_counts_as_relevance_proof(session):
    """Produktionsfehler: „Mongolian Trains“ scheiterte daran, dass unser Titel „Trainstories“ heißt."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "ambient"], description="Ambient zur Reise nach Mongolian.",
            topics=["https://en.wikipedia.org/wiki/Ambient_music"])
    intent = audience.build_intents(session, session.get(Video, "a"))[0]
    pool(session, "channel", "UCmong", "Mongolian Trains", subscribers=45000, item_count=120,
         description="Videos von Zugfahrten in Mongolian", query=intent["query"],
         details={"intent": {k: intent[k] for k in ("key", "kind", "label", "head", "context", "attestations")}
                  | {"video_id": "a"}})
    result = aq.collect(session, NOW, http=FakeHttp())
    verdict = next(v for v in result["pools"]["verdicts"] if v["title"] == "Mongolian Trains")
    assert verdict["kept"] is True, verdict["reason"]
    assert "mongolian" in (verdict["intent_hit"] or [])
    surface = session.scalar(select(TrafficSurface).where(TrafficSurface.key == "UCmong"))
    assert surface is not None and surface.evidence["intent"]["label"] == intent["label"]
    # Die Begruendung nennt die Evidenz, nicht nur die Wortgleichheit.
    assert surface.evidence["audience_fit"]["class"] in ("topic", "genre", "neighbourhood")
    assert "mongolian" in surface.evidence["audience_fit"]["topic"]


def test_a_single_attested_intent_still_needs_its_context_to_match(session):
    """Sonst würde „Good Times Full Episodes“ über ein einziges Tag zum Thema erklärt."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    weak = {"key": "a:good times", "kind": "topic_context", "label": "„good times“ + genre „ambient“",
            "head": ["good", "times"], "context": ["ambient"], "attestations": 1, "video_id": "a"}
    assert audience.matches(weak, {"good", "episodes", "seasons"}) is None
    assert audience.matches(weak, {"good", "times", "episodes"}) is not None
    strong = {**weak, "attestations": 2}
    assert audience.matches(strong, {"good", "episodes"}) is not None


# ---------------------------------------------------------------------------- Dashboard-Nachweis
def test_the_dashboard_shows_every_intent_with_its_evidence(session):
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "ambient"], description="Ambient zur Reise nach Mongolian.",
            topics=["https://en.wikipedia.org/wiki/Ambient_music"])
    view = aq.overview(session, NOW)
    rows = view["audience_intents"]
    assert rows, "ohne sichtbare Intents ist die Herkunft der Queries nicht prüfbar"
    first = rows[0]
    assert first["video"] == "Sealand Trainstories" and first["query"] and first["strength"] in ("belegt", "einfach belegt")
    assert first["evidence"] and all(item["label"] and item["detail"] for item in first["evidence"])
    assert first["head"] and "attestations" in first


def test_the_dashboard_markup_wires_the_intent_section():
    """Die Herkunft der Queries muss im Dashboard stehen, nicht nur im Log."""
    from pathlib import Path
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'id="trafficIntents"' in html and "audience_intents" in js
    assert "web_search" not in js, "die verworfene externe Suche ist aus dem Dashboard entfernt"


def test_the_channel_search_uses_the_topic_head_and_the_playlist_search_the_full_intent(session):
    """„trans mongolian“ findet Kanäle zum Thema; „trans mongolian ambient“ findet passende Playlists."""
    from app import discovery
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "ambient"], description="Ambient zur Reise nach Mongolian.",
            topics=["https://en.wikipedia.org/wiki/Ambient_music"])
    plans = discovery.pool_queries(session)
    playlist = [p for p in plans if p["search"] == "playlist"]
    channel = [p for p in plans if p["search"] == "channel"]
    assert playlist and channel, "beide Suchen laufen"
    assert playlist[0]["query"].split()[-1] == "ambient", "die Playlist-Suche nimmt den Kontext mit"
    for job in channel:
        head = job["intent"]["head"]
        # Mehrwortiger Themenkopf: die Kanalsuche nimmt ihn allein. Ein Wort allein waere zu breit,
        # deshalb bleibt dort der Kontext dabei.
        assert job["query"] == (" ".join(head) if len(head) >= 2 else job["intent"]["query"])
        assert len(job["query"].split()) >= 2, "ein Wort allein ist keine Suche"


# ---------------------------------------------------------------------------- die Produktionsfaelle
DESCRIPTION = """Trainstories ist die zweite Single unseres Albums.

Musik: Sanchez
Musicvideo: Factoria GmbH
Webpage: www.sealandmusic.ch
Released 2026. Check out our new album!
Eine Reise mit der Transmongolischen Eisenbahn, aufgenommen im Nachtzug."""


def test_credits_websites_and_sentence_fragments_never_become_intents(session):
    """Produktion lieferte webpage pop, factoria pop, sanchez pop, released pop, check rock, unser rock."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "ambient", "pop"], description=DESCRIPTION,
            topics=["https://en.wikipedia.org/wiki/Pop_music"])
    rows = {row["term"]: row for row in audience.profile_terms(session, session.get(Video, "a"))}
    for name in ("sanchez", "factoria", "webpage", "sealandmusic", "musicvideo"):
        assert name not in rows or rows[name]["category"] == "format", f"{name} ist kein Audience-Thema"
    for filler in ("released", "check", "unser", "good", "times", "long"):
        assert filler not in rows or rows[filler]["category"] == "format"
    heads = {" ".join(intent["head"]) for intent in audience.build_intents(session, session.get(Video, "a"))}
    assert not {h for h in heads if h in ("webpage", "factoria", "sanchez", "released", "check", "musicvideo")}
    assert any("mongolian" in head for head in heads), "das echte Motiv bleibt"


def test_shared_english_words_alone_are_never_an_audience_fit(session):
    """Produktionsfall: Trainstories → Young Nudy „Long Ride“ wegen long, out, ride."""
    session.get(Video, "a").title = "Sealand Trainstories"
    session.commit()
    profile(session, tags=["trans mongolian", "ambient"], description="Ambient aus dem Nachtzug. Check it out!")
    prof = audience.music_profile(session, session.get(Video, "a"))
    fit = audience.audience_fit({"long", "ride", "out", "young", "nudy"}, prof, tags=["rap", "trap"])
    assert fit["class"] is None and "keine gemeinsame Zielgruppe" in fit["why"]


def test_a_shared_genre_with_a_musical_candidate_is_a_real_fit(session):
    """Shine On → The Dead South: folk ist die Evidenz, good und out sind keine."""
    session.get(Video, "b").title = "Sealand Shine On"
    session.commit()
    profile(session, video_id="b", tags=["folk", "acoustic"], description="Ruhiger Folk mit Gitarre.",
            topics=["https://en.wikipedia.org/wiki/Folk_music"])
    prof = audience.music_profile(session, session.get(Video, "b"))
    fit = audience.audience_fit({"dead", "south", "good", "company", "folk", "out"}, prof,
                                tags=["folk", "bluegrass"])
    assert fit["class"] == "genre" and fit["genre"] == ["folk"]
    assert "folk" in fit["why"] and "good" not in fit["shared"] and "out" not in fit["shared"]
    # Ohne gemeinsames Genre bleibt von demselben Kandidaten nichts uebrig.
    session.get(Video, "b").title = "Sealand Shine On"
    plain = audience.audience_fit({"dead", "south", "good", "company", "out"}, prof, tags=["bluegrass"])
    assert plain["class"] is None
