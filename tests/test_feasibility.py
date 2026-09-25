"""Feasibility-Check: ein Experiment darf nur Ressourcen voraussetzen, deren Existenz belegt ist.

Der Produktionsfehler: der Vorschlag verlangte „bestehende, thematisch passende Sealand-Playlist wählen“,
obwohl der Kanal überhaupt keine Playlist hat. Das System hatte Playlists nie abgefragt.
"""
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select
from app import discovery, growth_engine as ge, history, regimes
from app.models import ChannelPlaylist, DiscoveryRun, GrowthAction, GrowthPlan, Reach, Video
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG
from test_growth_v5 import BASE
from test_actionable_growth import CHANNEL, CONF, details_for, starved

NO_PLAYLISTS = {"state": "none", "items": [], "checked_day": str(TODAY), "note": "Inventar geprueft: keine Playlist."}
UNKNOWN = {"state": "unknown", "items": [], "checked_day": None, "note": "Playlists wurden noch nicht abgefragt."}
SOURCE = CHANNEL["source"]


def test_a_channel_without_playlists_never_gets_a_choose_an_existing_playlist_step():
    """Der geforderte Regressionstest, geprueft ueber jede Aktion mit Schritten."""
    f = starved()
    for playlists in (NO_PLAYLISTS, UNKNOWN):
        channel = {**CHANNEL, "playlists": playlists}
        for action in ge.ACTIONS:
            steps = ge.experiment_steps(action, "Trainstories", f, None, channel) or []
            for step in steps:
                low = step.lower()
                assert not ("playlist" in low and ("bestehend" in low or "existierend" in low)), (action, step)
        # Und die Aktion selbst setzt niemals eine Playlist voraus.
        action, _ = ge.distribution_action(f, BASE, None, channel)
        assert action != "place_in_existing_playlist"
        assert ge.requirement(action, channel)["kind"] in ("source_video", "none")


def test_an_existing_playlist_is_named_only_when_the_inventory_reported_it():
    f = starved()
    available = {"state": "available", "checked_day": str(TODAY),
                 "items": [{"id": "PL1", "title": "Ambient Train Journeys", "item_count": 6, "privacy": "public"}]}
    # Ohne Quellvideo, aber mit belegter Playlist: Platzierung in genau dieser Playlist.
    channel = {"playlists": available, "source_candidates": [], "source": None}
    action, notes = ge.distribution_action(f, BASE, None, channel)
    assert action == "place_in_existing_playlist"
    steps = ge.experiment_steps(action, "Trainstories", f, None, channel)
    assert any("Ambient Train Journeys" in s for s in steps)
    assert ge.requirement(action, channel) == {"kind": "playlist", "verified": True,
                                               "named": ["Ambient Train Journeys"], "evidence": None}


def test_a_new_playlist_is_never_a_substitute_growth_action():
    """Eine Oberflaeche nur zum Messen anzulegen bringt keine zusaetzliche organische Reichweite."""
    f = starved()
    channel = {"playlists": NO_PLAYLISTS, "source_candidates": [], "source": None}
    action, notes = ge.distribution_action(f, BASE, None, channel)
    assert action == "observe"
    assert any("keine zusätzliche organische Reichweite" in n for n in notes)
    assert "create_playlist_context" in ge.EVIDENCE_ONLY and "probe_missing_evidence" in ge.EVIDENCE_ONLY
    # Ungeprueft ist nicht „nicht vorhanden“: dann wird gar nichts vorgeschlagen.
    unknown_action, unknown_notes = ge.distribution_action(f, BASE, None, {"playlists": UNKNOWN, "source_candidates": []})
    assert unknown_action == "observe" and any("Ressourcenlage ungeprüft" in n for n in unknown_notes)


def test_a_named_source_video_is_used_and_an_ambiguous_one_asks_the_human():
    f = starved()
    clear = {"playlists": NO_PLAYLISTS, "source_candidates": [SOURCE], "source": SOURCE}
    steps = ge.experiment_steps("link_from_own_video", "Trainstories", f, None, clear)
    assert any("Shine On" in s and "Endscreen" in s for s in steps)
    assert ge.choices_for("link_from_own_video", clear) is None
    # Zwei aehnlich gut ausgelieferte Videos: das System raet nicht, sondern legt beide mit Evidenz vor.
    second = {"video_id": "c", "title": "11AM", "impressions_7d": 800, "views_7d": 110,
              "evidence": "110 Views und 800 Impressions in der letzten bekannten Woche"}
    candidates = [SOURCE, second]
    ambiguous = {"playlists": NO_PLAYLISTS, "source_candidates": candidates, "source": ge.unique_source(candidates)}
    assert ambiguous["source"] is None, "kein klarer Vorsprung – keine Wahl durch das System"
    choices = ge.choices_for("link_from_own_video", ambiguous)
    assert choices["what"] == "source_video" and choices["required"] is True
    assert [o["title"] for o in choices["options"]] == ["Shine On", "11AM"]
    steps = ge.experiment_steps("link_from_own_video", "Trainstories", f, None, ambiguous)
    assert any("Shine On" in s and "11AM" in s and "wählt hier bewusst nicht" in s for s in steps)
    d = details_for("link_from_own_video", f, [], channel=ambiguous)
    assert d["needs_human_choice"] is True and d["choices"]["what"] == "source_video"
    # Ein deutlich besser ausgeliefertes Video wird dagegen benannt.
    weak = {"video_id": "c", "title": "11AM", "impressions_7d": 10, "views_7d": 1, "evidence": "1 View"}
    assert ge.unique_source([SOURCE, weak])["video_id"] == "b"


def test_only_delivered_videos_can_serve_as_a_source(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)
    histories = {h.video.id: h for h in history.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v],
                 "features": history.features_at(histories[v], TODAY)} for v in ("a", "b")]
    candidates = ge.source_candidates(contexts, "a")
    assert [c["video_id"] for c in candidates] == ["b"] and candidates[0]["views_7d"] > 0
    # Ein Video ohne jede Auslieferung ist kein Kandidat.
    contexts.append({"video": session.get(Video, "b"), "history": histories["b"],
                     "features": {"views_7d": 0, "impressions_7d": 0}})
    assert len(ge.source_candidates(contexts, "a")) == 1


def test_the_inventory_distinguishes_unknown_from_none(session):
    assert ge.playlist_inventory(session, TODAY)["state"] == "unknown"
    session.add(DiscoveryRun(day=TODAY, status="ok", stats={"playlists": 0, "playlists_checked_day": str(TODAY)}))
    session.commit()
    checked = ge.playlist_inventory(session, TODAY)
    assert checked["state"] == "none" and "keine Playlist" in checked["note"]
    session.add(ChannelPlaylist(id="PL1", title="Ambient", item_count=3, privacy="public",
                                first_seen_day=TODAY, last_seen_day=TODAY, checked_at=NOW))
    session.commit()
    assert ge.playlist_inventory(session, TODAY)["state"] == "available"
    # Veraltetes Inventar ohne Zeilen gilt wieder als unbekannt, nicht als "keine".
    session.query(ChannelPlaylist).delete()
    session.query(DiscoveryRun).delete()
    session.add(DiscoveryRun(day=TODAY-timedelta(days=30), status="ok",
                             stats={"playlists": 0, "playlists_checked_day": str(TODAY-timedelta(days=30))}))
    session.commit()
    assert ge.playlist_inventory(session, TODAY)["state"] == "unknown"


def test_the_plan_offers_the_named_link_experiment_end_to_end(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)       # Trainstories-artig: kaum Auslieferung
    seed_history(session, "b", days=400, base=120, seed=3)      # klar bestes eigenes Video
    end = TODAY-timedelta(days=LAG)
    for i in range(7):
        session.add(Reach(video_id="a", day=end-timedelta(days=i), impressions=6, ctr=.08, report_id="r"))
        session.add(Reach(video_id="b", day=end-timedelta(days=i), impressions=2000, ctr=.05, report_id="r"))
    session.add(DiscoveryRun(day=TODAY, status="ok", stats={"playlists": 0, "playlists_checked_day": str(TODAY)}))
    session.commit()
    rows, _ = history.build(history.load(session), 168, LAG)
    base = regimes.baselines(rows)
    histories = {h.video.id: h for h in history.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v], "features": history.features_at(histories[v], TODAY),
                 "regime": regimes.classify(history.features_at(histories[v], TODAY), base), "forecasts": [], "experiments": [],
                 "recommendation": {"confidence": CONF}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    entry = next(q for q in plan["queue"] if q["video_id"] == "a")
    assert entry["action"] in ("link_from_own_video", "probe_missing_evidence")
    assert entry["requires"]["kind"] == "source_video" and entry["requires"]["verified"] is True
    assert all("Playlist" not in s for s in entry["steps"]), "der Kanal hat keine Playlist"
    assert any(session.get(Video, "b").title in s for s in entry["steps"])
    stored = session.get(GrowthAction, entry["action_id"])
    assert stored.status == ge.PROPOSED and stored.payload["requires"]["verified"] is True


def test_a_weakly_delivered_source_is_flagged_before_the_work_starts():
    f = starved()
    weak = {"video_id": "b", "title": "Shine On", "impressions_7d": 11, "views_7d": 5,
            "evidence": "5 Views und 11 Impressions in der letzten bekannten Woche"}
    channel = {"playlists": NO_PLAYLISTS, "source_candidates": [weak], "source": weak}
    steps = ge.experiment_steps("link_from_own_video", "Trainstories", f, None, channel)
    assert any("Erwartungsmanagement" in s and "nur 5 Views" in s for s in steps)
    strong = {**SOURCE, "views_7d": 120}
    fine = ge.experiment_steps("link_from_own_video", "Trainstories", f, None,
                               {"playlists": NO_PLAYLISTS, "source_candidates": [strong], "source": strong})
    assert not any("Erwartungsmanagement" in s for s in fine)


def test_a_resource_held_by_a_running_experiment_is_never_a_source(monkeypatch, session):
    """Produktionskonflikt: beide neuen Maßnahmen wollten Trainstories als Quellvideo veraendern.

    Bei Maßnahme #8 wird Trainstories gemessen und der Endscreen von Shine On veraendert. Beide Seiten
    sind geschuetzt: am gemessenen Video und an der veraenderten Quelle darf nichts angefasst werden,
    solange die Messung laeuft.
    """
    running = GrowthAction(video_id="a", created_day=TODAY-timedelta(days=2), created_at=NOW, version=ge.VERSION,
                           state="needs_distribution", action="probe_missing_evidence",
                           target_metric="impressions_7d", window_days=14,
                           evaluate_after=TODAY+timedelta(days=16), status="running",
                           started_day=TODAY-timedelta(days=2), started_at=NOW, lever_class="internal_link",
                           traffic_source="END_SCREEN",
                           payload={"requires": {"kind": "source_video", "named": ["Video b"],
                                                 "video_ids": ["b"], "verified": True}})
    session.add(running)
    session.commit()
    locked = ge.locked_resources(session)
    assert set(locked) == {"a", "b"}, "gemessenes Video und veraenderte Quelle"
    assert locked["a"]["role"] == "gemessenes Video" and locked["b"]["role"] == "veraenderte Quellressource"
    contexts = [{"history": SimpleNamespace(video=session.get(Video, v)),
                 "features": {"impressions_7d": 900, "views_7d": 40}} for v in ("a", "b")]
    assert ge.source_candidates(contexts, "c", locked) == [], "keine geschuetzte Ressource als Quelle"
    assert ge.delivery_leader(contexts, "c", locked) is None
    # Und die Blockade wird benannt, statt stillschweigend nichts zu liefern.
    channel = {"source_candidates": [], "playlists": {"state": "unknown", "items": []},
               "locked_sources": [{"title": "Sealand Trainstories", "role": "gemessenes Video", "action_id": 8,
                                   "until": "2026-10-11"}]}
    lever, note = ge.feasible_lever(channel)
    assert lever is None
    assert "Keine freie eigene Quellressource" in note and "#8" in note and "2026-10-11" in note
    # Ohne laufendes Experiment bleibt alles wie vorher.
    running.status = "evaluated"
    session.commit()
    assert ge.locked_resources(session) == {}
    assert [c["video_id"] for c in ge.source_candidates(contexts, "c", ge.locked_resources(session))] == ["a", "b"]
