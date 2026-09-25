"""Qualitätsregeln vor dem ersten echten Experiment.

1. Wortüberschneidung ist keine Themengleichheit: „Shine On“ und „Shine Jesus Shine“ teilen ein Token.
   Ein fremder Kontext darf Beschreibung oder Playlist erst vorgeben, wenn das Thema belegt ist.
2. Genau ein veränderlicher Hebel je Experiment – sonst ist das Ergebnis keiner Ursache zuzuordnen.
"""
from datetime import timedelta
from app import discovery, growth_engine as ge
from test_learning_v4 import TODAY
from test_growth_v5 import BASE, features
from test_actionable_growth import CHANNEL, CONF, details_for, queue_row, starved


# ---------------------------------------------------------------------------- 1) semantische Plausibilität
def test_a_single_shared_word_is_not_a_topic():
    # Der konkrete Produktionsfall: „Shine On“ neben „Shine Jesus Shine (with lyrics)“.
    usable, reason = discovery.topical_context(["shine"], "multi_signal_proxy", relevance=1.0, members=1, channels=1)
    assert usable is False and "Wortgleichheit ist keine" in reason
    # Auch mit eigenen Traffic-Daten bleibt ein einzelnes Wort zu wenig.
    usable, _ = discovery.topical_context(["shine"], "multi_signal_proxy", relevance=1.0, own_data=True)
    assert usable is False
    # Ein einzelnes Nachbarvideo ist keine unabhaengige Bestaetigung, selbst bei zwei gemeinsamen Begriffen.
    usable, reason = discovery.topical_context(["train", "journey"], "multi_signal_proxy", relevance=.8, members=1, channels=1)
    assert usable is False and "zu wenig unabhängige Bestätigung" in reason
    # Mehrere unabhaengige Nachbarvideos aus mehreren Kanaelen mit mehreren gemeinsamen Begriffen: plausibel.
    usable, reason = discovery.topical_context(["train", "journey"], "multi_signal_proxy", relevance=.8,
                                              members=discovery.MIN_CONTEXT_MEMBERS, channels=discovery.MIN_CONTEXT_CHANNELS)
    assert usable is True and "thematisch plausibel" in reason
    # Eigene Analytics belegen das Thema unmittelbar – dort hat YouTube die Verbindung selbst gemeldet.
    assert discovery.topical_context(["shine"], "own_analytics", relevance=.5)[0] is True
    # Markenbegriffe zaehlen nicht als gemeinsames Thema.
    assert discovery.topical_context(["sealand", "shine"], "multi_signal_proxy", relevance=1.0, members=5, channels=3)[0] is False


def test_an_unproven_context_never_dictates_wording_or_playlist():
    f = starved()
    unproven = {"score": discovery.MULTI_PROXY_SCORE_CAP, "kind": "suggested", "key": "shine jesus shine",
                "audience": "Shine Jesus Shine (with lyrics)", "gap": "suggested_opportunity", "actionable": True,
                "evidence_level": "multi_signal_proxy", "demand_source": "public_proxy", "context_usable": False,
                "context_reason": "Nur 1 gemeinsames Stichwort (shine): Wortgleichheit ist keine Themengleichheit"}
    action, notes = ge.choose_action("needs_distribution", f, {"signals": []}, BASE, [], {}, unproven, CHANNEL)
    assert action != "target_suggested_cluster" and action == "link_from_own_video"
    assert any("bleibt Hypothese und lenkt keinen Wortlaut" in n for n in notes)
    assert any("Wortgleichheit" in n for n in notes)
    d = details_for(action, f, notes, unproven)
    assert all("Shine Jesus Shine" not in step for step in d["steps"]), "kein fremder Titel im Wortlaut"
    assert all("shine jesus shine" not in step.lower() for step in d["steps"])
    # Und im Plan ist der Status der Chance ausdruecklich als Hypothese ausgewiesen.
    row = queue_row("b", "Shine On", "needs_distribution", action, 60, priority=1,
                    external={"context_usable": False, "context_reason": unproven["context_reason"],
                              "evidence_level": "multi_signal_proxy", "key": "shine jesus shine",
                              "audience": "Shine Jesus Shine (with lyrics)", "actionable": True, "score": 70,
                              "demand_source": "public_proxy", "kind": "suggested", "gap": "suggested_opportunity"})
    entry = ge.daily_plan([row], TODAY, {})["queue"][0]
    assert entry["context_status"] == "Hypothese – gibt keinen Wortlaut vor"
    assert "Wortgleichheit" in entry["context_reason"]


def test_a_proven_context_may_guide_the_choice_of_an_existing_playlist():
    f = starved()
    proven = {"score": 72, "kind": "cluster", "key": "train journey", "audience": "train journey",
              "gap": "suggested_opportunity", "actionable": True, "evidence_level": "multi_signal_proxy",
              "demand_source": "own_traffic_plus_public_proxy", "context_usable": True,
              "context_reason": "3 Nachbarvideos aus 2 Kanälen teilen 2 Begriffe"}
    # Bei belegtem Thema lenkt die Chance die Aktion – hier den Wortlaut-Test.
    assert ge.choose_action("needs_distribution", f, {"signals": []}, BASE, [], {}, proven, CHANNEL)[0] == "target_suggested_cluster"
    # Ein belegtes Thema darf die Auswahl einer real existierenden Playlist leiten – hier existiert keine,
    # also wird auch keine genannt; die belegte Quelle ist das Video.
    d = details_for("link_from_own_video", f, [], proven)
    assert all("Playlist" not in step for step in d["steps"])
    playlisted = details_for("place_in_existing_playlist", f, [], proven, channel={
        **CHANNEL, "playlists": {"state": "available", "items": [{"id": "PL1", "title": "Trainstories & Ambient",
                                                                  "item_count": 4, "privacy": "public"}]}})
    assert any("Trainstories & Ambient" in step for step in playlisted["steps"])


# ---------------------------------------------------------------------------- 2) ein Hebel je Experiment
def test_every_experiment_names_exactly_one_primary_lever():
    f = starved()
    for action in ge.ACTIONS:
        assert ge.LEVERS.get(action), f"kein Hebel definiert: {action}"
    d = details_for("link_from_own_video", f, [])
    assert d["primary_lever"].startswith("Endscreen/Infokarte")
    assert "eigene Experimente" in d["one_lever_note"] or "eigenes Experiment" in d["one_lever_note"]
    assert "Beschreibungstext ausrichten" in d["deferred_levers"]


def test_the_distribution_test_changes_nothing_on_the_video_itself():
    f = starved()
    for action in ("link_from_own_video", "probe_missing_evidence"):
        d = details_for(action, f, [])
        for protected in ("Titel", "Thumbnail", "Beschreibung"):
            assert protected in d["do_not_change"], (action, protected)
        assert any("Titel, Thumbnail und Beschreibung bleiben unverändert" in step for step in d["steps"]), action
        # Die Beschreibung wird nicht angefasst: kein Schritt fordert eine Textergaenzung.
        assert not any("ergänzen" in step for step in d["steps"]), action


def test_the_wording_experiment_leaves_playlist_and_packaging_alone():
    f = starved()
    proven = {"key": "train journey music", "audience": "train journey music", "actionable": True,
              "evidence_level": "own_analytics", "context_usable": True, "score": 80, "gap": "search_opportunity"}
    d = details_for("target_search_opportunity", f, [], proven)
    assert d["primary_lever"] == "Wortlaut in Beschreibung und Kapiteln"
    assert any("Titel, Thumbnail und Playlist-Platzierung bleiben" in step for step in d["steps"])
    assert "Playlist-Platzierung" in d["deferred_levers"] and "Titel" in d["deferred_levers"]


# ---------------------------------------------------------------------------- Messbarkeit / Priorisierung
def test_a_low_baseline_limits_the_certainty_not_the_growth_measure():
    """Zwei verschiedene Fragen: darf die Hypothese laufen, und wie sicher ist die spaetere Aussage?"""
    ok, reason = ge.measurable({"views_7d": 0, "impressions_7d": 2})
    assert ok is False and "Keine messbare Ausgangsbasis" in reason
    assert ge.measurable({"views_7d": 18, "impressions_7d": 42})[0] is True
    assert ge.measurable({"views_7d": 5, "impressions_7d": 11})[0] is True
    strong = {"actionable": True, "evidence_level": "own_analytics", "score": 61.1, "gap": "suggested_opportunity",
              "kind": "suggested", "key": "nachbarschaft"}
    rows = [queue_row("a", "Trainstories", "needs_distribution", "link_from_own_video", 70, priority=1,
                      baseline={"views_7d": 18, "impressions_7d": 42, "ctr_7d": .08}),
            queue_row("b", "Shine On", "needs_distribution", "target_suggested_cluster", 60, priority=2,
                      baseline={"views_7d": 1, "impressions_7d": 5, "ctr_7d": .06}, external=strong),
            queue_row("c", "11AM Album - Teaser", "needs_distribution", "link_from_own_video", 80, priority=3,
                      baseline={"views_7d": 0, "impressions_7d": 1, "ctr_7d": None})]
    plan = ge.daily_plan(rows, TODAY, {})
    ids = [q["video_id"] for q in plan["queue"]]
    assert ids[0] == "b", "die belegte Reichweiten-Hypothese steht vorn"
    assert "a" in ids and "c" not in ids, "ohne Hypothese und ohne messbare Basis keine Aufgabe"
    shine = next(q for q in plan["queue"] if q["video_id"] == "b")
    assert shine["reliability"] == ge.INDICATIVE and "Keine messbare Ausgangsbasis" in shine["reliability_note"]
    assert next(q for q in plan["queue"] if q["video_id"] == "a")["reliability"] == ge.RELIABLE
    assert [x["video_id"] for x in plan["not_testable"]] == ["c"]


def test_every_growth_action_names_its_discovery_surface_and_certainty():
    """Jede Maßnahme sagt, welche algorithmische Flaeche sie treffen soll und wie sicher die Aussage ist."""
    from pathlib import Path
    for lever in ge.GROWTH_LEVERS:
        assert ge.DISCOVERY_SURFACES.get(lever), lever
    assert "Suggested" in ge.DISCOVERY_SURFACES["target_suggested_cluster"]
    assert "Suche" in ge.DISCOVERY_SURFACES["target_search_opportunity"]
    assert "Browse" in ge.DISCOVERY_SURFACES["packaging_for_audience"]
    row = queue_row("a", "Trainstories", "needs_distribution", "target_suggested_cluster", 70, priority=1,
                    external={"actionable": True, "evidence_level": "own_analytics", "score": 61.1,
                              "gap": "suggested_opportunity", "kind": "suggested", "key": "nachbarschaft"})
    entry = ge.daily_plan([row], TODAY, {})["queue"][0]
    assert entry["discovery_surface"].startswith("Suggested/Related")
    assert entry["reliability"] in (ge.RELIABLE, ge.INDICATIVE)
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "Algorithmische Fläche" in js and "Aussagekraft" in js


def test_the_suggested_cluster_measure_touches_exactly_one_existing_asset():
    """Produktionsfehler: der Vorschlag nannte Beschreibung UND Playlist-Benennung – ohne vorhandene Playlist."""
    f = starved(traffic_total_7d=4)
    external = {"audience": "Mihai Ciobanu Anii cei mai dragi din viata mea", "key": "nachbarschaft",
                "actionable": True, "evidence_level": "own_analytics", "score": 61.1, "context_usable": True,
                "gap": "suggested_opportunity", "kind": "suggested"}
    channel = {"playlists": {"state": "none", "items": [], "note": "geprüft: keine Playlist"},
               "source_candidates": [], "source": None}
    steps = ge.experiment_steps("target_suggested_cluster", "Sealand - Shine On", f, external, channel)
    assert any("Beschreibungszeilen" in s and "Shine On" in s for s in steps)
    assert any("keine Playlist" in s and "angelegt oder umbenannt" in s for s in steps)
    assert not any("Playlist-Benennung spiegeln" in s for s in steps)
    assert ge.LEVERS["target_suggested_cluster"] == "Wortlaut der Beschreibung dieses Videos"
    assert any("Playlist-Benennung" in x for x in ge.DEFERRED_LEVERS["target_suggested_cluster"])
    # Auch der Wirkmechanismus beschreibt nur diesen einen Hebel.
    mechanism = details_for("target_suggested_cluster", f, [], external, title="Sealand - Shine On",
                           channel=channel)["reason"]
    assert "Beschreibung dieses Videos" in mechanism and "Endscreens" not in mechanism
