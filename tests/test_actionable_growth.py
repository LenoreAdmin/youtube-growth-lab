"""Actionable-Growth-Sprint: mehrstufige Evidenz, Verteilung vor Verpackung, Experiment-Queue, Lernschleife.

Kern der Umstellung: `observe` ist kein Endzustand mehr, wenn die Auslieferung praktisch null ist,
und eine Chance ohne eigene Suchbegriff-Details kann durch mehrere unabhaengige Evidenzfamilien
zu einem Experiment werden. Ein einzelner Proxy bleibt Hypothese.
"""
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select
from app import discovery, growth_engine as ge, history, regimes
from app.models import DiscoveryOpportunity, DiscoveryQuery, GrowthAction, GrowthPlan, Reach, Video
from test_learning_v4 import seed_history, wire, NOW, TODAY, LAG
from test_growth_v5 import BASE, features

CONF = {"level": "low", "n_videos": 3, "n_rows": 500, "n_origins": 400}


def starved(**changes):
    """Der Trainstories-Fall: funktionierendes Paket (8 % CTR), brauchbare Retention, kaum Auslieferung."""
    return features(**{"ctr_7d": .08, "impressions_7d": 40, "views_7d": 18, "views_28d": 70, "traffic_total_7d": 18,
                       "retention_avg": .55, "velocity_7d": 2.6, **changes})


def details_for(action, f, notes, external=None, title="Trainstories"):
    board = ge.scores(f, {"regime": "stable"}, BASE, [], None, None)
    return ge.action_details(action, "needs_distribution", f, {"regime": "stable"}, BASE, board, {"candidate": False, "signals": []},
                            None, CONF, notes, [], external,
                            channel={"delivery_leader": {"video_id": "b", "title": "Shine On", "impressions_7d": 900, "views_7d": 120}},
                            title=title)


# ---------------------------------------------------------------------------- Evidenzstufen
def test_evidence_levels_need_several_independent_families():
    assert discovery.classify_evidence({"own_term_demand"}) == "own_analytics"
    assert discovery.classify_evidence({"search_probe"}) == "weak_proxy"
    assert discovery.classify_evidence(set()) == "none"
    assert discovery.classify_evidence({"own_traffic_mix", "search_probe"}) == "multi_signal_proxy"
    # Ein einzelner Proxy bleibt Hypothese und kann keine Aktion ausloesen.
    assert discovery.grade(90, "weak_proxy") <= discovery.PROXY_SCORE_CAP < ge.EXTERNAL_MIN_SCORE
    # Mehrere unabhaengige Familien duerfen ein Experiment tragen, bleiben aber unter eigener Analytics.
    assert discovery.grade(90, "multi_signal_proxy") == discovery.MULTI_PROXY_SCORE_CAP >= ge.EXTERNAL_MIN_SCORE
    assert discovery.MULTI_PROXY_SCORE_CAP < discovery.LEVEL_CAPS["own_analytics"]
    assert discovery.grade(90, "own_analytics") == 90 and discovery.grade(50, "none") is None
    assert set(discovery.ACTIONABLE_LEVELS) == {"own_analytics", "multi_signal_proxy"}
    assert discovery.demand_source({"own_traffic_mix", "search_probe"}) == "own_traffic_plus_public_proxy"
    assert discovery.uncertainty("weak_proxy", {"search_probe"}) == "hoch"


def test_circular_and_generic_seeds_cannot_be_lifted_by_proxies_alone():
    # Aus dem eigenen Titel gewonnener Seed: zwei oeffentliche Proxies heben ihn nicht.
    assert discovery.classify_evidence({"search_probe", "neighbour_metadata"}, circular=True) == "weak_proxy"
    assert discovery.classify_evidence({"search_probe", "own_traffic_mix"}, circular=True) == "multi_signal_proxy"
    # Ein-Wort-Seed ohne eigene Suchnachfrage ist keine Suchintention, egal wie viele Proxies es gibt.
    assert discovery.classify_evidence({"search_probe", "own_traffic_mix"}, generic=True) == "none"
    assert discovery.classify_evidence({"own_term_demand"}, generic=True) == "own_analytics"
    assert discovery.seed_quality("pop", "title", set())["generic"] is True
    assert discovery.seed_quality("pop", "own_search_term", set())["generic"] is False
    assert discovery.seed_quality("pop", "title", {"pop"})["generic"] is False
    assert discovery.seed_quality("train journey", "title_bigram", set())["circular"] is True
    assert discovery.seed_quality("train journey music", "own_search_term", set())["is_own_search_term"] is True
    assert discovery.missing_evidence({"search_probe"})
    assert discovery.missing_evidence(set(), generic=True)[0]["family"] == "specific_seed"


def test_one_observation_cannot_pose_as_two_families():
    probe_only = SimpleNamespace(via={"queries": ["train journey"]})
    # Ergebnismenge und Metadaten derselben Suchprobe bleiben eine Familie.
    assert discovery.item_routes(probe_only, has_metadata=True) == {"search_probe"}
    assert discovery.classify_evidence(discovery.item_routes(probe_only, has_metadata=True)) == "weak_proxy"
    own = SimpleNamespace(via={"suggested_source": ["own_traffic"]})
    assert discovery.item_routes(own, has_metadata=True) == {"neighbour_metadata"}
    assert discovery.item_routes(own, has_metadata=False) == set()
    both = SimpleNamespace(via={"suggested_source": ["own_traffic"], "queries": ["x"]})
    assert discovery.classify_evidence(discovery.item_routes(both, has_metadata=True)) == "multi_signal_proxy"


def test_generic_legacy_seeds_are_retired_and_never_probed_again(session):
    for query, source in (("pop", "title"), ("note", "tag"), ("train journey", "title_bigram"), ("shine", "own_search_term")):
        session.add(DiscoveryQuery(query=query, source=source, seed_video_id="a", priority=500.0, created_at=NOW,
                                  probe_count=0, failures=0, results={}))
    session.commit()
    assert discovery.retire_generic_seeds(session, TODAY) == 2
    rows = {q.query: q for q in session.scalars(select(DiscoveryQuery))}
    for generic in ("pop", "note"):
        assert rows[generic].priority == discovery.RETIRED_PRIORITY
        assert rows[generic].next_probe_day > TODAY+timedelta(days=365)
    assert rows["train journey"].priority == 500.0 and rows["train journey"].next_probe_day is None
    assert rows["shine"].priority == 500.0, "echter eigener Suchbegriff bleibt, auch als ein Wort"
    assert discovery.retire_generic_seeds(session, TODAY) == 0, "idempotent"


# ---------------------------------------------------------------------------- Verteilung vor Verpackung
def test_distribution_comes_first_when_the_package_already_works():
    f = starved()
    ok, why = ge.packaging_ok(f, BASE)
    assert ok is True and "8.0 %" in why
    scarce, detail = ge.distribution_scarce(f, BASE)
    assert scarce is True and detail["impressions_7d"] == 40 and detail["threshold"] == ge.LOW_IMPRESSIONS_7D
    state = ge.state_of(f, {"regime": "stable"}, BASE, {"candidate": False})
    assert state == "needs_distribution" and state not in ge.INACTIVE_STATES
    action, notes = ge.choose_action(state, f, {"signals": []}, BASE, [], {})
    assert action == "distribute_playlist_context"
    assert action not in ("test_thumbnail", "test_title", "test_title_thumbnail"), "kein erzwungener Packaging-Test"
    assert action not in ge.PASSIVE_ACTIONS and any("Paket ist nicht der Engpass" in n for n in notes)
    d = details_for(action, f, notes)
    assert "Titel" in d["do_not_change"] and "Thumbnail" in d["do_not_change"]
    assert d["target_metric"] == "discovery_views_7d" and d["window_days"] == 14
    assert any("Playlist" in s for s in d["steps"]) and any("Shine On" in s for s in d["steps"])
    # Thema und Oberflaeche bleiben getrennt: die Traffic-Route ist kein Playlist-Thema.
    assert any("Playlist zum Thema dieses Videos" in s and "Ansatzpunkt laut eigenen Daten: Empfehlungen" in s for s in d["steps"])
    # Mit benannter Audience steht das Thema im Schritt, nicht die Route.
    named = details_for("distribute_playlist_context", f, [], {"audience": "train journey music", "actionable": True,
                                                              "evidence_level": "multi_signal_proxy", "score": 65})
    assert any("Playlist zu „train journey music“" in s for s in named["steps"])
    assert d["baseline"]["impressions_7d"] == 40 and d["baseline"]["ctr_7d"] == .08
    assert d["executed_automatically"] is False and "ändert nichts" in d["read_only"]
    assert d["route"]["key"] == "traffic_suggested"


def test_a_high_impression_low_ctr_video_still_gets_a_packaging_test():
    # Gegenprobe: viele Impressions, schwache CTR – hier ist das Paket der Engpass.
    f = features(ctr_7d=.01, impressions_7d=9000, views_7d=90, traffic_total_7d=90)
    assert ge.packaging_ok(f, BASE)[0] is False
    assert ge.distribution_scarce(f, BASE)[0] is False
    assert ge.state_of(f, {"regime": "declining"}, BASE, {"candidate": False}) == "needs_packaging_test"
    assert ge.choose_action("needs_packaging_test", f, {"signals": []}, BASE, [], {})[0] == "test_thumbnail"


def test_observe_is_never_the_end_state_when_delivery_is_practically_zero():
    stable = features()
    assert ge.state_of(stable, {"regime": "stable"}, BASE, {"candidate": False}) == "observe"
    # Dasselbe Video mit fast keiner Auslieferung: handelbarer Zustand statt Beobachten.
    assert ge.state_of(starved(), {"regime": "stable"}, BASE, {"candidate": False}) == "needs_distribution"
    # Auch ohne belastbare Kanal-Baseline bleibt es handelbar.
    assert ge.state_of(starved(), {"regime": "insufficient_data"}, BASE, {"candidate": False}) == "needs_distribution"
    # Schutz bleibt unangetastet: ein Breakout wird nie zum Distributionsfall umgedeutet.
    assert ge.state_of(starved(), {"regime": "breakout"}, BASE, {"candidate": False}) == "protect_momentum"
    assert ge.state_of(starved(paid_views_32d=5), {"regime": "paid_excluded"}, BASE, {"candidate": False}) == "paid_excluded"


def test_without_a_route_the_system_names_the_missing_evidence_and_probes_it():
    f = starved(traffic_total_7d=4)            # unter MIN_ROUTE_VIEWS: keine Route benennbar
    assert ge.known_route(f) is None
    action, notes = ge.choose_action("needs_distribution", f, {"signals": []}, BASE, [], {})
    assert action == "probe_missing_evidence" and action not in ge.PASSIVE_ACTIONS
    assert any("Keine belastbare Hypothese" in n for n in notes)
    d = details_for(action, f, notes)
    assert d["target_metric"] == "impressions_7d", "gemessen wird die Auslieferung selbst"
    assert any("Aggregationsschwelle" in m for m in d["missing_evidence"])
    assert any("Playlist" in s for s in d["steps"]) and any("Endscreen" in s for s in d["steps"])
    assert "Impressions" in d["success_criterion"] and "Titel" in d["do_not_change"]
    # Fehlende Evidenz aus V6 wird woertlich uebernommen, wenn vorhanden.
    external = {"missing_evidence": [{"family": "own_term_demand", "what": "Eigene Suchbegriff-Details"}], "actionable": False,
                "evidence_level": "weak_proxy", "score": 40}
    assert ge.choose_action("needs_distribution", f, {"signals": []}, BASE, [], {}, external)[0] == "probe_missing_evidence"
    assert details_for("probe_missing_evidence", f, [], external)["missing_evidence"] == ["Eigene Suchbegriff-Details"]


def test_multi_signal_proxy_may_steer_the_action_but_a_single_proxy_may_not():
    f = starved()
    strong = {"score": discovery.MULTI_PROXY_SCORE_CAP, "kind": "search", "key": "train journey music", "gap": "search_opportunity",
              "evidence_level": "multi_signal_proxy", "actionable": True, "demand_source": "own_traffic_plus_public_proxy",
              "audience": "train journey music", "families": ["own_traffic_mix", "search_probe"], "uncertainty": "mittel"}
    action, notes = ge.choose_action("needs_distribution", f, {"signals": []}, BASE, [], {}, strong)
    assert action == "target_search_opportunity" and "multi_signal_proxy" in notes[0]
    weak = {**strong, "score": discovery.PROXY_SCORE_CAP, "evidence_level": "weak_proxy", "actionable": False,
            "families": ["search_probe"]}
    # Der einzelne Proxy lenkt nichts – die Verteilung bleibt trotzdem das Thema, nicht Beobachten.
    assert ge.choose_action("needs_distribution", f, {"signals": []}, BASE, [], {}, weak)[0] == "distribute_playlist_context"


# ---------------------------------------------------------------------------- Queue
def queue_row(video_id, title, state, action, score, **changes):
    return {**{"video_id": video_id, "title": title, "state": state, "regime": "stable", "breakout": False, "action": action,
               "opportunity_score": score, "viewer_score": score, "subscriber_score": score, "revival": False, "revival_signals": [],
               "confidence": "low", "reason": "r", "notes": [], "window_days": 14, "target_metric": "discovery_views_7d",
               "success_criterion": "+15 %", "stop_criterion": "zurücknehmen", "objective": "Discovery",
               "do_not_change": ["Titel", "Thumbnail"], "next_evaluation": str(TODAY+timedelta(days=17)), "held_since": None,
               "momentum": None, "paid_status": "organic", "paid": {}, "paid_note": "nie beworben", "priority": 1,
               "action_id": 100+len(video_id), "action_status": "proposed", "started_day": None,
               "steps": ["Playlist setzen", "Endscreen verlinken"], "baseline": {"views_7d": 18, "impressions_7d": 40, "ctr_7d": .08},
               "missing_evidence": [], "route": {"key": "traffic_suggested", "label": "Empfehlungen", "share": .4, "views_7d": 18},
               "external": None}, **changes}


def test_queue_is_short_one_per_video_and_leaves_winners_and_running_tests_alone():
    rows = [queue_row("a", "Trainstories", "needs_distribution", "distribute_playlist_context", 70),
            queue_row("b", "Shine On", "needs_distribution", "probe_missing_evidence", 60),
            queue_row("c", "Dritter", "protect_momentum", "protect_no_change", 95),
            queue_row("d", "Vierter", "needs_discovery", "improve_discovery", 55, action_status="running",
                      started_day=str(TODAY-timedelta(days=3)), held_since=str(TODAY-timedelta(days=3))),
            queue_row("e", "Fuenfter", "needs_distribution", "distribute_playlist_context", 50),
            queue_row("f", "Sechster", "observe", "observe", 45)]
    for i, r in enumerate(rows):
        r["priority"] = i+1
    plan = ge.daily_plan(rows, TODAY, {}, results=[{"video_id": "a", "action": "distribute_playlist_context", "outcome": "positive",
                                                   "created_day": str(TODAY-timedelta(days=20)), "metric": "discovery_views",
                                                   "before": 5, "after": 20, "relative_change": 3.0}])
    queue = plan["queue"]
    assert 0 < len(queue) <= ge.QUEUE_LIMIT == 3
    # Belegte Hypothesen zuerst; "b" beschafft nur Evidenz und rutscht dahinter, trotz hoeherem Score als "e".
    assert [q["video_id"] for q in queue] == ["a", "e", "b"], "nach aktiver Prioritaet, je Video hoechstens eines"
    assert len({q["video_id"] for q in queue}) == len(queue)
    assert all(q["video_id"] not in ("c", "d", "f") for q in queue), "geschuetzt, laufend oder passiv bleibt draussen"
    assert plan["now_do"]["video_id"] == "a" and plan["now_do"]["rank"] == 1
    assert queue[-1]["action"] == "probe_missing_evidence"
    entry = queue[0]
    for key in ("steps", "baseline", "success_criterion", "stop_criterion", "window_days", "evaluate_after", "measure_from",
                "expected_signal", "evidence", "do_not_change", "objective", "target_metric", "why"):
        assert entry[key], key
    assert entry["executed_automatically"] is False and "Read-only" in entry["note"]
    assert entry["evidence"]["own_route"]["label"] == "Empfehlungen" and entry["evidence"]["confidence"] == "low"
    assert plan["running_experiments"] and plan["running_experiments"][0]["video_id"] == "d"
    assert plan["running_experiments"][0]["started_day"] == str(TODAY-timedelta(days=3))
    assert "nichts weiter an diesem Video" in plan["running_experiments"][0]["note"]
    assert plan["results"][0]["outcome"] == "positive"
    assert "öchstens 3" in plan["queue_note"]
    # Geschuetztes Video bleibt sichtbar geschuetzt und taucht nicht als Aufgabe auf.
    assert [p["video_id"] for p in plan["protected"]] == ["c"]


def test_queue_is_empty_and_says_why_when_nothing_is_actionable():
    rows = [queue_row("c", "Dritter", "protect_momentum", "protect_no_change", 95, priority=1),
            queue_row("f", "Sechster", "observe", "observe", 45, priority=2)]
    plan = ge.daily_plan(rows, TODAY, {})
    assert plan["queue"] == [] and plan["now_do"] is None
    assert plan["active_status"] == "none" and "kein ausführbares Experiment" in plan["queue_note"]


# ---------------------------------------------------------------------------- Lernschleife
def test_outcomes_adjust_evidence_weights_only_with_enough_decided_cases(session):
    def opportunity(day, level, outcome):
        session.add(DiscoveryOpportunity(day=day, kind="search", key=f"k{day}{outcome}", video_id="a", gap="search_opportunity",
                                         scores={"external_audience_score": 60.0}, components={}, status="evaluated", outcome=outcome,
                                         evidence={"evidence_level": level}, evaluation={}, evaluated_at=NOW))
    for i in range(2):
        opportunity(TODAY-timedelta(days=40+i), "multi_signal_proxy", "positive")
    session.commit()
    weights, level_weights, record = discovery.memory_weights(session)
    assert level_weights == {} and weights == {}, "unter fuenf entschiedenen Faellen kein Gewichtsschub"
    entry = record["by_evidence_level"]["multi_signal_proxy"]
    assert entry["weight"] == 1.0 and "nichts bewiesen" in entry["basis"]
    for i in range(4):
        opportunity(TODAY-timedelta(days=20+i), "multi_signal_proxy", "negative")
    session.commit()
    _, level_weights, record = discovery.memory_weights(session)
    assert 0.85 <= level_weights["multi_signal_proxy"] <= 1.15
    assert record["by_evidence_level"]["multi_signal_proxy"]["decided"] == 6
    # Eine abgestrafte Evidenzstufe faellt unter die Schwelle und lenkt keine Aktion mehr.
    punished = discovery.grade(discovery.MULTI_PROXY_SCORE_CAP, "multi_signal_proxy", {"multi_signal_proxy": 0.85})
    assert punished < ge.EXTERNAL_MIN_SCORE


def test_growth_actions_are_tracked_per_evidence_level(session):
    for i, (level, outcome) in enumerate((("multi_signal_proxy", "negative"), ("multi_signal_proxy", "negative"), ("own_analytics", "positive"))):
        session.add(GrowthAction(video_id="a", created_day=TODAY-timedelta(days=30+i), version=ge.VERSION,
                                 state="needs_distribution", action="distribute_playlist_context", target_metric="discovery_views_7d",
                                 window_days=14, evaluate_after=TODAY-timedelta(days=1), status="evaluated", outcome=outcome,
                                 evaluated_at=NOW, evaluation={"detail": {"metric": "discovery_views", "before": 10, "after": 4,
                                                                          "relative_change": -0.6}},
                                 payload={"audience": {"evidence_level": level, "demand_source": "public_proxy"}}))
    session.commit()
    record = ge.track_record(session)
    assert record["distribute_playlist_context"]["n"] == 3
    levels = record["by_evidence_level"]
    assert levels["multi_signal_proxy"]["negative"] == 2 and levels["own_analytics"]["positive"] == 1
    assert levels["multi_signal_proxy"]["proven"] is False
    results = ge.recent_results(session)
    assert results and results[0]["evidence_level"] in ("multi_signal_proxy", "own_analytics")
    assert results[0]["metric"] == "discovery_views" and "keine nachgewiesene Ursache" in results[0]["note"]


def test_impressions_are_a_measurable_experiment_target():
    row = SimpleNamespace(target_metric="impressions_7d", action="probe_missing_evidence")
    empty = {"paid_views": 0, "observed_days": 7, "impressions": 5, "views": 3, "ctr": None, "watch_minutes": 1,
             "discovery_views": 1, "subscribers": 0}
    grown = {**empty, "impressions": 400}
    assert ge._outcome(row, empty, grown, BASE)[0] == "positive"
    assert ge._outcome(row, grown, empty, BASE)[0] == "negative"
    assert ge._outcome(row, {**empty, "paid_views": 3}, grown, BASE)[0] == "inconclusive"
    assert ge._outcome(row, {**empty, "impressions": None}, grown, BASE)[0] == "inconclusive"


# ---------------------------------------------------------------------------- Ende zu Ende
def test_a_starved_video_produces_an_executable_experiment_in_the_plan(monkeypatch, session):
    wire(monkeypatch, session)
    seed_history(session, "a", days=400, base=3, trend=0)       # ~20 Views/Woche: kaum Auslieferung
    seed_history(session, "b", days=400, base=120, seed=3)      # gut ausgeliefertes Video als Verlinkungsquelle
    end = TODAY-timedelta(days=LAG)
    for i in range(7):
        session.add(Reach(video_id="a", day=end-timedelta(days=i), impressions=6, ctr=.08, report_id="r"))
        session.add(Reach(video_id="b", day=end-timedelta(days=i), impressions=2000, ctr=.05, report_id="r"))
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
    entry = next((q for q in plan["queue"] if q["video_id"] == "a"), None)
    assert entry is not None, f"kein ausführbares Experiment: {[ (r['video_id'], r['state'], r['action']) for r in plan['ranking'] ]}"
    assert entry["action"] in ("distribute_playlist_context", "probe_missing_evidence")
    assert entry["steps"] and entry["executed_automatically"] is False
    assert entry["baseline"]["impressions_7d"] == 42 and entry["window_days"] == 14
    assert "Titel" in entry["do_not_change"] and "Thumbnail" in entry["do_not_change"]
    stored = session.scalar(select(GrowthAction).where(GrowthAction.video_id == "a", GrowthAction.status == "proposed"))
    assert stored.action == entry["action"] and stored.payload["steps"] == entry["steps"]
    assert stored.state == "needs_distribution"
