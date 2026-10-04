"""Content Intelligence: mehrere belegte Kandidaten aus eigenem Material, ohne erfundene Labels.

Zwei fachliche Grenzen werden hier festgenagelt:

1. **Kein semantisches Label.** Gespeichert und ausgegeben werden Messwerte. Nirgends steht „Refrain“,
   und die Entscheidung funktioniert ohne diese Benennung.
2. **Kein behaupteter Gewinner.** Ein Video liefert mehrere messbar unterschiedliche Kandidaten mit
   exakten Zeiten; die Reihenfolge entsteht aus Messwerten und aus tatsaechlicher Distribution – und
   solange zu wenige Shorts veroeffentlicht sind, sagt das System genau das.
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import content as ci
from app import media
from app.models import (ContentCandidate, ContentLine, ContentSection, Daily, MediaAsset, TrafficDaily,
                        Video, VideoRetention, utcnow)

TODAY = date(2026, 10, 3)
FORBIDDEN = ("refrain", "chorus", "strophe", "verse", "bridge", "hook-teil", "drop")

# Fuenf gemessene Abschnitte mit deutlich verschiedenen Eigenschaften. Keine Benennung, nur Zahlen.
SECTIONS = [
    dict(idx=0, start_seconds=0.0, end_seconds=30.0, energy=0.5, energy_rel=0.80, repetition_strength=0.30,
         repetition_group=0, vocal_presence=0.40, novelty=None, visual_cuts=2, visual_cut_density=0.07,
         brightness=0.3, boundary_cut_distance=0.0),
    dict(idx=1, start_seconds=30.0, end_seconds=72.0, energy=0.9, energy_rel=1.40, repetition_strength=0.90,
         repetition_group=1, vocal_presence=0.80, novelty=0.60, visual_cuts=6, visual_cut_density=0.14,
         brightness=0.6, boundary_cut_distance=0.1),
    dict(idx=2, start_seconds=72.0, end_seconds=110.0, energy=0.7, energy_rel=1.00, repetition_strength=0.50,
         repetition_group=2, vocal_presence=0.60, novelty=0.30, visual_cuts=4, visual_cut_density=0.11,
         brightness=0.5, boundary_cut_distance=0.4),
    dict(idx=3, start_seconds=110.0, end_seconds=160.0, energy=0.85, energy_rel=1.30, repetition_strength=0.85,
         repetition_group=1, vocal_presence=0.75, novelty=0.55, visual_cuts=5, visual_cut_density=0.10,
         brightness=0.55, boundary_cut_distance=0.2),
    dict(idx=4, start_seconds=160.0, end_seconds=214.0, energy=0.4, energy_rel=0.60, repetition_strength=0.20,
         repetition_group=3, vocal_presence=0.10, novelty=0.40, visual_cuts=1, visual_cut_density=0.02,
         brightness=0.2, boundary_cut_distance=0.6),
]

CURVE = [{"at": 0.0, "ratio": .9}, {"at": .2, "ratio": .5}, {"at": .4, "ratio": .8},
         {"at": .6, "ratio": .82}, {"at": .8, "ratio": .3}, {"at": 1.0, "ratio": .2}]


def seed_asset(session, video_id="a", asset_id="sha-a", duration=214.0, lines=(), curve=CURVE):
    session.get(Video, video_id).duration_seconds = duration
    session.add(MediaAsset(id=asset_id, video_id=video_id, path=f"/masters/{video_id}.mp4", bytes=1234,
                           duration_seconds=duration, has_video=True, has_audio=True, status="analysed",
                           tools={"ffmpeg": "ffmpeg", "librosa": "0.10"}, analysed_at=utcnow()))
    session.flush()
    for section in SECTIONS:
        session.add(ContentSection(asset_id=asset_id, **section))
    for idx, line in enumerate(lines):
        session.add(ContentLine(asset_id=asset_id, idx=idx, **line))
    if curve:
        session.add(VideoRetention(video_id=video_id, rows=list(curve), window_start=TODAY-timedelta(days=89),
                                   window_end=TODAY-timedelta(days=3), fetched_day=TODAY))
    session.commit()
    return session.get(Video, video_id)



def seed_renders(session, video, folder=r"C:\Sealand\shorts"):
    """Die fertig geschnittenen Dateien – ohne sie gibt es keine veroeffentlichbare Maßnahme."""
    stored = ci.store(session, video, ci.candidates(session, video), TODAY)
    for candidate in stored:
        row = session.get(ContentCandidate, candidate["candidate_id"])
        row.render_path = f"{folder}\short-{row.id}-{int(row.start_seconds)}s.mp4"
    session.commit()
    # Erneut lesen, damit die Pfade in den zurueckgegebenen Eintraegen stehen.
    return ci.store(session, video, ci.candidates(session, video), TODAY)


def test_one_video_yields_several_measurably_different_candidates(session):
    """Das Erfolgskriterium: mehrere nachvollziehbar unterschiedliche, konkret produzierbare Kandidaten."""
    video = seed_asset(session)
    found = ci.candidates(session, video)
    assert len(found) >= 3, found
    for candidate in found:
        # Konkret produzierbar heisst: exakte Zeiten innerhalb der Shorts-Grenze.
        assert candidate["end_seconds"] > candidate["start_seconds"]
        assert ci.SHORT_MIN <= candidate["duration_seconds"] <= ci.SHORT_MAX
        assert candidate["end_seconds"] <= video.duration_seconds
        assert candidate["evidence"], candidate
    starts = [c["start_seconds"] for c in found]
    assert len(set(starts)) == len(starts)
    for first in range(len(found)):
        for second in range(first+1, len(found)):
            gap = abs(found[first]["start_seconds"]-found[second]["start_seconds"])
            assert gap >= ci.DISTINCT_SECONDS, (found[first], found[second])
    # Und sie unterscheiden sich nicht nur in der Zeit, sondern in gemessenen Eigenschaften.
    profiles = {tuple(sorted((k, v) for k, v in c["properties"].items())) for c in found}
    assert len(profiles) == len(found)


def test_no_semantic_label_is_ever_invented(session):
    """Wiederholung plus Energie wird nicht zu „Refrain“ – gespeichert sind Messwerte."""
    video = seed_asset(session)
    found = ci.candidates(session, video)
    assert found
    for candidate in found:
        assert set(candidate["properties"]) <= set(ci.MEASURED)
        text = " ".join(candidate["evidence"]).lower()+" "+str(candidate.get("hook") or "").lower()
        for word in FORBIDDEN:
            assert word not in text, (word, candidate["evidence"])
    # Die Spalten selbst tragen keine Semantik, nur Messwerte.
    assert not {c.name for c in ContentSection.__table__.columns} & {"chorus", "section_type", "label"}
    assert "repetition_strength" in {c.name for c in ContentSection.__table__.columns}


def test_candidates_exist_without_any_lyrics(session):
    """Songtexte sind optional: ohne Text entsteht kein Hook, aber sehr wohl ein Kandidat."""
    video = seed_asset(session)
    found = ci.candidates(session, video)
    assert found and all(c["hook"] is None for c in found)
    assert all(c["properties"]["energy_rel"] is not None for c in found)
    assert ci.vocabulary(session, video) == set()


def test_an_aligned_line_becomes_a_verbatim_hook_and_a_guess_never_does(session):
    """Nur ausgerichteter eigener Text ist zitierfaehig; unsichere Erkennung bleibt ohne Wirkung."""
    lines = [dict(start_seconds=30.4, end_seconds=34.0, text="Shine on through the night",
                  confidence=0.93, source="aligned"),
             dict(start_seconds=72.2, end_seconds=75.0, text="etwas kaum verstandenes",
                  confidence=0.31, source="asr")]
    video = seed_asset(session, lines=lines)
    found = {c["start_seconds"]: c for c in ci.candidates(session, video)}
    assert found[30.0]["hook"] == "Shine on through the night"
    assert found[30.0]["hook_source"] == "aligned"
    assert found[72.0]["hook"] is None, "eine unsichere Erkennung darf kein Hook werden"
    # Der eigene Wortlaut praegt das Packaging nur, wo er belegt ist.
    words = ci.vocabulary(session, video)
    assert "shine" in words and "kaum" not in words


def test_learning_stays_silent_until_enough_shorts_were_published(session):
    """Aus einem oder zwei Shorts wird nichts „gelernt“ – und das System sagt es."""
    seed_asset(session)
    learned = ci.priors(session, TODAY)
    assert learned["learning"] is False and learned["weights"] == {} and learned["basis"] == 0
    assert str(ci.MIN_PUBLISHED_FOR_PRIORS) in learned["note"]
    assert "Materialeigenschaften" in learned["note"]


def _publish(session, video, asset_id, start, end, properties, views, algorithmic, day):
    short_id = f"short-{int(start)}"
    session.add(Video(id=short_id, channel_id=video.channel_id, title=f"Short {start}",
                      published_at=utcnow(), duration_seconds=end-start))
    row = ContentCandidate(asset_id=asset_id, video_id=video.id, start_seconds=start, end_seconds=end,
                           properties=properties, evidence=["gemessen"], created_day=day)
    session.add(row)
    session.flush()
    ci.attach_published(session, row.id, short_id, day)
    session.add(Daily(video_id=short_id, day=day, views=views, watch_minutes=views*0.4,
                      average_duration=20.0, average_percentage=55.0, subscribers_gained=0,
                      subscribers_lost=0, likes=1, comments=0))
    session.add(TrafficDaily(video_id=short_id, day=day, source="SHORTS", views=algorithmic,
                             watch_minutes=algorithmic*0.4, paid=False))
    session.commit()
    return row


def test_learning_comes_from_real_distribution_of_the_published_shorts(session):
    """Die eigene Auslieferung der veroeffentlichten Shorts bestimmt, welche Eigenschaft vorne steht."""
    video = seed_asset(session)
    day = TODAY-timedelta(days=30)
    # Drei veroeffentlichte Shorts: hoher Gesangsanteil erhielt deutlich mehr algorithmische Auslieferung.
    _publish(session, video, "sha-a", 300.0, 340.0, {"vocal_presence": 0.9, "energy_rel": 1.2}, 900, 800, day)
    _publish(session, video, "sha-a", 400.0, 440.0, {"vocal_presence": 0.8, "energy_rel": 0.7}, 700, 600, day)
    _publish(session, video, "sha-a", 500.0, 540.0, {"vocal_presence": 0.1, "energy_rel": 1.3}, 50, 20, day)
    learned = ci.priors(session, TODAY)
    assert learned["learning"] is True and learned["basis"] == 3
    assert learned["weights"]["vocal_presence"] > 0, learned["weights"]
    assert "veroeffentlichten Shorts" in learned["note"]
    # Die Verbindung Kandidat -> veroeffentlichtes Asset ist der Trager des Lernens.
    published = list(session.scalars(select(ContentCandidate).where(ContentCandidate.published_video_id.is_not(None))))
    assert len(published) == 3 and all(p.status == "published" for p in published)


def test_learned_weights_actually_change_the_order(session):
    """Gelerntes wirkt. Die echte Distribution schlaegt die Heuristik, nicht umgekehrt.

    Zeigt die Auslieferung der veroeffentlichten Shorts, dass hohe Retention im Quellvideo gerade
    nicht zu Reichweite im Shorts-Feed fuehrt, muss die Auswahl dem folgen – sonst waere das Lernen
    eine Behauptung.
    """
    video = seed_asset(session)
    neutral = [c["start_seconds"] for c in ci.candidates(session, video, None, {})]
    weighted = [c["start_seconds"] for c in ci.candidates(session, video, None, {"retention_rel": -0.9})]
    assert neutral and weighted
    assert neutral[0] != weighted[0], (neutral, weighted)
    assert set(neutral) == set(weighted), "dieselben Kandidaten, andere Reihenfolge"


def test_a_candidate_is_stored_so_distribution_can_be_attributed_later(session):
    """Ohne gespeicherten Kandidaten gaebe es spaeter keinen Bezug zwischen Short und Hook."""
    video = seed_asset(session)
    found = ci.candidates(session, video)
    stored = ci.store(session, video, found, TODAY)
    session.commit()
    assert all(c["candidate_id"] for c in stored)
    rows = list(session.scalars(select(ContentCandidate)))
    assert len(rows) == len(found)
    # Derselbe Lauf an einem anderen Tag legt keine Duplikate an.
    again = ci.store(session, video, ci.candidates(session, video), TODAY+timedelta(days=1))
    session.commit()
    assert {c["candidate_id"] for c in again} == {c["candidate_id"] for c in stored}
    assert len(list(session.scalars(select(ContentCandidate)))) == len(rows)


def test_material_reports_candidates_together_with_the_learning_state(session):
    video = seed_asset(session)
    found = ci.material(session, video, {"score": 70}, TODAY)
    assert found["primary"]["rank"] == 1
    assert len(found["alternatives"]) == len(found["candidates"])-1
    assert found["learning"]["learning"] is False
    assert ci.material(session, session.get(Video, "b"), None, TODAY) is None


def test_the_window_ends_on_a_measured_boundary_or_at_the_shorts_limit(session):
    video = seed_asset(session)
    found = {c["start_seconds"]: c for c in ci.candidates(session, video)}
    boundaries = {s["start_seconds"] for s in SECTIONS} | {s["end_seconds"] for s in SECTIONS}
    for candidate in found.values():
        on_boundary = candidate["end_seconds"] in boundaries
        at_limit = abs(candidate["duration_seconds"]-ci.SHORT_MAX) < 0.01
        at_end = abs(candidate["end_seconds"]-video.duration_seconds) < 0.01
        assert on_boundary or at_limit or at_end, candidate
        # Der Grund fuer das Ende wird benannt und nicht verwechselt.
        reason = next(e for e in candidate["evidence"] if e.startswith("Ende "))
        assert reason == ("Ende auf einer gemessenen Abschnittsgrenze" if on_boundary else
                          "Ende am Ende des Materials" if at_end else
                          f"Ende bei {ci.SHORT_MAX:.0f} s Shorts-Grenze"), (reason, candidate)


def test_retention_is_one_factor_and_its_absence_is_stated(session):
    """Retention wird genutzt, wo sie gemessen ist – und ihr Fehlen wird ausgesprochen, nicht ersetzt."""
    video = seed_asset(session, curve=None)
    found = ci.candidates(session, video)
    assert found, "ohne Kurve traegt das Material die Auswahl allein"
    assert all(c["properties"]["retention_rel"] is None for c in found)
    assert all(any("keine Retentionspunkte" in e for e in c["evidence"]) for c in found)
    session.query(VideoRetention).delete()
    with_curve = seed_asset(session, video_id="b", asset_id="sha-b", duration=214.0)
    measured = ci.candidates(session, with_curve)
    assert any(c["properties"]["retention_rel"] is not None for c in measured)
    assert any("Retention" in e for c in measured for e in c["evidence"])


def test_media_processing_never_runs_hosted(monkeypatch):
    """Die Verarbeitung gehoert lokal hin: in einer Vercel-Funktion gibt es keine Dateien."""
    monkeypatch.setattr(media, "settings", SimpleNamespace(hosted=True))
    with pytest.raises(media.Unavailable):
        media.local_only()
    monkeypatch.setattr(media, "settings", SimpleNamespace(hosted=False))
    assert media.local_only() is None


def test_a_file_is_mapped_to_a_video_only_when_the_duration_is_unambiguous(session):
    """Geraten wird nicht: mehrdeutig heisst ohne Zuordnung, nicht irgendeine Zuordnung."""
    video, note = media.match_video(session, 600.0)        # "a" und "b" sind beide 600 s lang
    assert video is None and "Mehrdeutig" in note
    session.get(Video, "a").duration_seconds = 214.0
    session.commit()
    video, note = media.match_video(session, 214.3)
    assert video is not None and video.id == "a" and note is None
    missing, note = media.match_video(session, 9999.0)
    assert missing is None and "Kein veroeffentlichtes Video" in note
    unknown, note = media.match_video(session, None)
    assert unknown is None and "ohne lesbare Dauer" in note


def test_the_content_module_carries_no_media_dependency():
    """`app.content` laeuft in einer Vercel-Funktion und darf nichts Mediennahes importieren."""
    import ast
    source = ast.parse(open("app/content.py", encoding="utf-8").read())
    imported = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"librosa", "soundfile", "scenedetect", "faster_whisper", "cv2", "PIL",
                           "subprocess", "imageio_ffmpeg"}, imported


def test_produce_for_opportunity_delivers_one_publishable_short(monkeypatch, session):
    """Der Abnahmetest: JETZT TUN enthaelt genau eine sofort ausfuehrbare Handlung.

    Datei, Titel, Beschreibung und die Anweisung „veroeffentlichen“ – keine Kandidatenliste, keine
    Messwerte, keine Auswahl und keine Analyseaufgabe.
    """
    from app import growth_engine as ge, history as hist, regimes as reg
    from app.models import DiscoveryOpportunity, GrowthAction, GrowthPlan
    from test_learning_v4 import seed_history, wire, NOW, TODAY as T, LAG
    from test_actionable_growth import CONF, details_for, starved
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=6, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)
    video = seed_asset(session, lines=[dict(start_seconds=110.3, end_seconds=114.0,
                                            text="Shine on through the night",
                                            confidence=0.94, source="aligned")],
                       curve=[{"at": round(i/40, 3), "ratio": 0.3+0.5*(i in range(20, 32))} for i in range(41)])
    seed_renders(session, video)
    session.add(GrowthAction(video_id="a", created_day=T-timedelta(days=3), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence",
                             target_metric="impressions_7d", window_days=14,
                             evaluate_after=T+timedelta(days=12), status="running",
                             started_day=T-timedelta(days=3), started_at=NOW, lever_class="internal_link",
                             payload=details_for("probe_missing_evidence", starved(), ["laeuft"])))
    session.add(DiscoveryOpportunity(day=T, kind="suggested", key="nachbarschaft", video_id="a",
                                     gap="suggested_opportunity", scores={"external_audience_score": 70.0},
                                     components={}, evidence={"evidence_level": "own_analytics", "actionable": True,
                                                              "context_usable": True, "title": "Nachbarcluster"},
                                     status="open"))
    session.commit()
    rows, _ = hist.build(hist.load(session), 168, LAG)
    base = reg.baselines(rows)
    histories = {h.video.id: h for h in hist.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v],
                 "features": hist.features_at(histories[v], T),
                 "regime": reg.classify(hist.features_at(histories[v], T), base), "forecasts": [],
                 "experiments": [], "recommendation": {"confidence": CONF}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    assert len(plan["queue"]) == 1, "genau eine Aktion"
    entry = plan["queue"][0]
    assert entry["action"] == "produce_for_opportunity"
    brief = entry["brief"]
    assert brief["action"] == "Diesen Short veroeffentlichen"
    upload = brief["upload"]
    assert upload["file"].endswith(".mp4") and upload["title"]
    assert ci.SHORT_MIN <= upload["duration_seconds"] <= ci.SHORT_MAX
    # Die Handlung steht in den Schritten, mit Datei und Titel – nichts zum Auswaehlen.
    joined = " ".join(entry["steps"])
    assert upload["file"] in joined and upload["title"] in joined
    assert "Video-ID oder den Link" in joined
    # Und das Eintragen der Video-ID ist angelegt.
    assert entry["publish"]["endpoint"].endswith(f"/api/growth/actions/{entry['action_id']}/published")
    assert entry["publish"]["field"] == "video"
    # Kein Kandidatenangebot, keine Messwerte als Ausgabe.
    assert "candidates" not in brief and "properties" not in repr(brief)
    assert not any(word in repr(entry["steps"]).lower() for word in ("retention", "energie", "wiederholung"))


def test_the_title_uses_our_own_words_and_never_a_machine_guess(session):
    """Ein geratener Songtext darf nie im Titel stehen; ein ausgerichteter darf es."""
    video = seed_asset(session, lines=[dict(start_seconds=30.4, end_seconds=34.0, text="Shine on tonight",
                                            confidence=0.81, source="asr")])
    guessed = next(c for c in ci.candidates(session, video) if c["start_seconds"] == 30.0)
    title = ci.youtube_title(video, guessed)
    assert "Shine on tonight" not in title
    assert title == f"{video.title} – {ci.EXCERPT}"
    session.query(ContentLine).delete()
    session.add(ContentLine(asset_id="sha-a", idx=0, start_seconds=30.4, end_seconds=34.0,
                            text="Shine on tonight", confidence=0.95, source="aligned"))
    session.commit()
    aligned = next(c for c in ci.candidates(session, video) if c["start_seconds"] == 30.0)
    assert ci.youtube_title(video, aligned).startswith("„Shine on tonight“")
    # Die Beschreibung nennt nur Belegtes: das eigene Video und seine Adresse.
    description = ci.youtube_description(video, aligned)
    assert f"https://youtu.be/{video.id}" in description and video.title in description


def test_without_a_rendered_file_there_is_no_action(session):
    """Eine Maßnahme, die eine nicht existierende Datei nennt, ist keine Maßnahme."""
    from app import growth_engine as ge
    video = seed_asset(session)
    found = ge.content_material(session, video, {"score": 70}, TODAY)
    assert found is not None and found["upload"] is None, "ohne Schnitt kein Paket"
    external = {"audience": "Nachbarcluster", "evidence_level": "own_analytics", "actionable": True,
                "context_usable": True, "score": 70}
    assert ge.brief_for(session, "produce_for_opportunity", video, external,
                        {"content": found, "lifetime_views": 1200}) is None
    assert ge.content_steps(video.title, {"content": found}, "zum Thema", "halten") is None
    options = ge.reach_options("needs_distribution", starved_features(), external,
                               {"content": found, "lifetime_views": 1200, "segment": None})
    assert all(o["action"] != "produce_for_opportunity" for o in options), "ohne Datei keine Produktion"


def starved_features():
    from test_actionable_growth import starved
    return starved()


def test_without_a_media_asset_nothing_about_content_is_claimed(monkeypatch, session):
    """Production ohne zugeordnete Datei: saubere Degradation, keine erfundene Inhaltsaussage.

    Das ist der Zustand, in dem Production direkt nach dem Deployment laeuft. Der Vorschlag darf dann
    genau das sagen, was die Retentionskurve hergibt – keine Kandidatenliste, keine Eigenschaften,
    keinen Hook und keinen Lernstand.
    """
    from app import growth_engine as ge
    video = session.get(Video, "a")
    video.duration_seconds = 214.0
    session.add(VideoRetention(video_id="a", rows=list(CURVE), window_start=TODAY-timedelta(days=89),
                               window_end=TODAY-timedelta(days=3), fetched_day=TODAY))
    session.commit()
    # Kein MediaAsset: die Inhaltsschicht liefert nichts und wirft nichts.
    assert ci.material(session, video, {"score": 70}, TODAY) is None
    assert ge.content_material(session, video, {"score": 70}, TODAY) is None
    assert ci.vocabulary(session, video) == set()
    channel = {"segment": ge.strong_segment(session, video), "content": None, "lifetime_views": 1200}
    external = {"audience": "Nachbarcluster", "evidence_level": "own_analytics", "actionable": True,
                "context_usable": True, "score": 70}
    brief = ge.brief_for(session, "produce_for_opportunity", video, external, channel)
    assert "candidates" not in brief and "learning" not in brief
    assert "Kandidatenmaterial" in brief["material"]
    assert brief["hook"] is None and brief["review"] == ge.REVIEW_REQUIRED
    # Und die Schritte bleiben die alten, ohne Verweis auf eine Datei oder einen Render-Befehl.
    steps = ge.content_steps(video.title, channel, "zum Thema dieses Videos", "halten")
    assert steps and not any("--render" in s or "app.cli published" in s for s in steps)
    # Die Reihenfolge-Faktoren vertragen das Fehlen ebenso.
    factors = ge.reach_outlook({"action": "produce_for_opportunity", "brief": None, "baseline": {},
                                "external": external, "lifetime_views": 1200})["factors"]
    assert factors["content_candidates"] == 0 and factors["learning_basis"] is None


def test_the_source_baseline_is_not_used_to_judge_a_video_that_does_not_exist_yet(session):
    """Die leere Woche des Quellvideos ist kein Befund ueber einen noch nicht produzierten Short."""
    from app import growth_engine as ge
    ok, why = ge.measurable({"views_7d": 0, "impressions_7d": 0}, "views_7d", "produce_for_opportunity")
    assert ok is False, "ohne eigene Zahlen bleibt die Aussage indikativ"
    assert "existiert noch nicht" in why and "nicht" in why
    assert "nur „unklar“" not in why, "die alte Begruendung galt dem Quellvideo"
    # Fuer eine Aenderung am Quellvideo gilt die Ausgangsbasis unveraendert.
    ok, why = ge.measurable({"views_7d": 0, "impressions_7d": 0}, "views_7d", "repackage_for_reach")
    assert ok is False and "nur „unklar“" in why


def test_an_open_recommendation_from_an_earlier_day_stays_executable(monkeypatch, session):
    """Der Production-Fehler: die Doppelungssperre liess den offenen Vorschlag aus dem Plan fallen.

    Eine Empfehlung, die gestern entstanden und nie gestartet wurde, muss heute weiter im Plan stehen –
    mit derselben Action-ID, damit ein spaeterer Start zuordenbar bleibt, und mit den heute gemessenen
    Kandidaten. Sonst existiert die Maßnahme nur als Datenbankzeile und ist nicht ausfuehrbar.
    """
    from app import growth_engine as ge, history as hist, regimes as reg
    from app.models import DiscoveryOpportunity, GrowthAction, GrowthPlan
    from test_learning_v4 import seed_history, wire, NOW, TODAY as T, LAG
    from test_actionable_growth import CONF, details_for, starved
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=6, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)
    video = seed_asset(session, curve=[{"at": round(i/40, 3), "ratio": 0.3+0.5*(i in range(20, 32))}
                                       for i in range(41)])
    seed_renders(session, video)
    # Ein laufendes Experiment haelt das Video, wie #8/#15 in Production.
    session.add(GrowthAction(video_id="a", created_day=T-timedelta(days=9), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence",
                             target_metric="impressions_7d", window_days=14,
                             evaluate_after=T+timedelta(days=5), status="running",
                             started_day=T-timedelta(days=9), started_at=NOW, lever_class="internal_link",
                             payload=details_for("probe_missing_evidence", starved(), ["laeuft"])))
    # Und ein offener Produktionsvorschlag von gestern, nie gestartet.
    stale = GrowthAction(video_id="a", created_day=T-timedelta(days=1), created_at=NOW, version=ge.VERSION,
                         state="needs_distribution", action="produce_for_opportunity",
                         target_metric="views_7d", window_days=30,
                         evaluate_after=T+timedelta(days=29), status="proposed", lever_class="content",
                         payload={"reason": "von gestern"})
    session.add(stale)
    session.add(DiscoveryOpportunity(day=T, kind="suggested", key="nachbarschaft", video_id="a",
                                     gap="suggested_opportunity", scores={"external_audience_score": 70.0},
                                     components={}, evidence={"evidence_level": "own_analytics", "actionable": True,
                                                              "context_usable": True, "title": "Nachbarcluster"},
                                     status="open"))
    session.commit()
    stale_id = stale.id
    rows, _ = hist.build(hist.load(session), 168, LAG)
    base = reg.baselines(rows)
    histories = {h.video.id: h for h in hist.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v],
                 "features": hist.features_at(histories[v], T),
                 "regime": reg.classify(hist.features_at(histories[v], T), base), "forecasts": [],
                 "experiments": [], "recommendation": {"confidence": CONF}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    entry = next((q for q in plan["queue"] if q["action"] == "produce_for_opportunity"), None)
    assert entry is not None, f"Vorschlag aus dem Plan gefallen: {[(q['video_id'], q['action']) for q in plan['queue']]}"
    # Dieselbe Zeile, fortgeschrieben – kein Duplikat.
    assert entry["action_id"] == stale_id
    assert len(list(session.scalars(select(GrowthAction)
                                    .where(GrowthAction.action == "produce_for_opportunity")))) == 1
    # Und mit den heute gemessenen Kandidaten statt der Nutzlast von gestern.
    assert entry["brief"]["upload"]["file"].endswith(".mp4")
    assert entry["why"] != "von gestern"
    assert session.get(GrowthAction, stale_id).payload["reason"] != "von gestern"


def test_entering_the_published_video_closes_the_loop(session):
    """Nach dem Upload traegt der Kanalinhaber die ID ein – danach misst das System selbst.

    Mehr ist von ihm nicht zu tun: keine Auswahl, keine Analyse, kein Datensammeln. Das Eintragen
    startet das Messfenster und verbindet den Short mit dem Kandidaten, aus dem er entstanden ist.
    """
    from app import growth_engine as ge
    from app.models import GrowthAction
    video = seed_asset(session)
    stored = seed_renders(session, video)
    candidate_id = stored[0]["candidate_id"]
    action = GrowthAction(video_id="a", created_day=TODAY, created_at=utcnow(), version=ge.VERSION,
                          state="needs_distribution", action="produce_for_opportunity",
                          target_metric="views_7d", window_days=30, evaluate_after=TODAY+timedelta(days=33),
                          status=ge.PROPOSED, lever_class="content",
                          payload={"brief": {"upload": {"candidate_id": candidate_id}},
                                   "baseline": {}, "steps": ["hochladen"]})
    session.add(action)
    session.commit()
    # Aus einem Link wird die ID erkannt; aus Unsinn nicht.
    assert ge.youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert ge.youtube_id("https://youtu.be/dQw4w9WgXcQ?si=abc") == "dQw4w9WgXcQ"
    assert ge.youtube_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert ge.youtube_id("kein link") is None
    # Das neue Video ist noch nicht synchronisiert: die ID wartet in der Maßnahme.
    result = ge.publish_action(session, action.id, "https://youtube.com/shorts/dQw4w9WgXcQ")
    session.commit()
    assert result["published_video_id"] == "dQw4w9WgXcQ" and result["linked"] is False
    assert session.get(GrowthAction, action.id).status == ge.RUNNING, "das Messfenster laeuft"
    assert session.get(ContentCandidate, candidate_id).published_video_id is None
    # Sobald der Sync das Video kennt, haengt es sich selbst ein.
    session.add(Video(id="dQw4w9WgXcQ", channel_id=video.channel_id, title="Sealand Short",
                      published_at=utcnow(), duration_seconds=40))
    session.commit()
    assert ci.attach_pending(session, TODAY) == [(candidate_id, "dQw4w9WgXcQ")]
    session.commit()
    row = session.get(ContentCandidate, candidate_id)
    assert row.published_video_id == "dQw4w9WgXcQ" and row.status == "published"
    assert session.get(GrowthAction, action.id).payload["pending_video_id"] is None
    # Ein zweiter Lauf verdoppelt nichts.
    assert ci.attach_pending(session, TODAY) == []


def test_the_stored_action_carries_the_candidate_so_the_link_can_be_made(monkeypatch, session):
    """Der zweite Production-Bug: der Brief lag nur im Plan, nie in der gespeicherten Maßnahme.

    Beim Eintragen der Video-ID fand sich deshalb keine Kandidaten-ID (`candidate=None`), der Short
    wurde als laufend registriert, aber nie seinem Ausschnitt zugeordnet – der Lernpfad blieb leer.
    """
    from app import growth_engine as ge, history as hist, regimes as reg
    from app.models import DiscoveryOpportunity, GrowthAction, GrowthPlan
    from test_learning_v4 import seed_history, wire, NOW, TODAY as T, LAG
    from test_actionable_growth import CONF, details_for, starved
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=6, trend=0)
    seed_history(session, "b", days=400, base=120, seed=3)
    video = seed_asset(session, curve=[{"at": round(i/40, 3), "ratio": 0.3+0.5*(i in range(20, 32))}
                                       for i in range(41)])
    seed_renders(session, video)
    session.add(GrowthAction(video_id="a", created_day=T-timedelta(days=3), created_at=NOW, version=ge.VERSION,
                             state="needs_distribution", action="probe_missing_evidence",
                             target_metric="impressions_7d", window_days=14,
                             evaluate_after=T+timedelta(days=12), status="running",
                             started_day=T-timedelta(days=3), started_at=NOW, lever_class="internal_link",
                             payload=details_for("probe_missing_evidence", starved(), ["laeuft"])))
    session.add(DiscoveryOpportunity(day=T, kind="suggested", key="nachbarschaft", video_id="a",
                                     gap="suggested_opportunity", scores={"external_audience_score": 70.0},
                                     components={}, evidence={"evidence_level": "own_analytics", "actionable": True,
                                                              "context_usable": True, "title": "Nachbarcluster"},
                                     status="open"))
    session.commit()
    rows, _ = hist.build(hist.load(session), 168, LAG)
    base = reg.baselines(rows)
    histories = {h.video.id: h for h in hist.load(session)}
    contexts = [{"video": session.get(Video, v), "history": histories[v],
                 "features": hist.features_at(histories[v], T),
                 "regime": reg.classify(hist.features_at(histories[v], T), base), "forecasts": [],
                 "experiments": [], "recommendation": {"confidence": CONF}} for v in ("a", "b")]
    ge.run(session, NOW, contexts, base)
    session.expire_all()
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.id.desc())).plan
    entry = plan["queue"][0]
    stored = session.get(GrowthAction, entry["action_id"])
    # Die gespeicherte Maßnahme traegt den Brief und damit den Kandidaten.
    candidate_id = stored.payload["brief"]["upload"]["candidate_id"]
    assert candidate_id == entry["brief"]["upload"]["candidate_id"]
    # Und das Eintragen verbindet tatsaechlich.
    session.add(Video(id="jZq_Cko_BCw", channel_id=video.channel_id, title="Sealand Short",
                      published_at=utcnow(), duration_seconds=56))
    session.commit()
    result = ge.publish_action(session, stored.id, "jZq_Cko_BCw")
    session.commit()
    assert result["candidate_id"] == candidate_id and result["linked"] is True
    assert session.get(ContentCandidate, candidate_id).published_video_id == "jZq_Cko_BCw"


def test_an_action_without_the_candidate_in_its_payload_is_still_linked(session):
    """Die bereits laufende Production-Maßnahme hat keinen Brief in der Nutzlast – sie muss trotzdem
    zugeordnet werden, ohne eine neue Maßnahme zu erzeugen."""
    from app import growth_engine as ge
    from app.models import GrowthAction
    video = seed_asset(session)
    stored = seed_renders(session, video)
    expected = ci.selected(stored)["candidate_id"]
    action = GrowthAction(video_id="a", created_day=TODAY, created_at=utcnow(), version=ge.VERSION,
                          state="needs_distribution", action="produce_for_opportunity",
                          target_metric="views_7d", window_days=30, evaluate_after=TODAY+timedelta(days=33),
                          status=ge.RUNNING, started_day=TODAY, started_at=utcnow(), lever_class="content",
                          payload={"baseline": {}, "steps": ["hochladen"]})     # kein brief
    session.add(action)
    session.commit()
    assert ci.candidate_for(session, "a") == expected
    result = ge.publish_action(session, action.id, "jZq_Cko_BCw")
    session.commit()
    assert result["candidate_id"] == expected
    assert session.get(GrowthAction, action.id).payload["pending_candidate_id"] == expected
    # Das Video ist noch nicht synchronisiert: der naechste Lauf haengt es ein.
    session.add(Video(id="jZq_Cko_BCw", channel_id=video.channel_id, title="Sealand Short",
                      published_at=utcnow(), duration_seconds=56))
    session.commit()
    assert ci.attach_pending(session, TODAY) == [(expected, "jZq_Cko_BCw")]
