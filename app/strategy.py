"""Strategy engine: turns regime, signals, backtest quality and experiment memory into auditable decisions.

Every statement is descriptive. Associations are never presented as causes, and
paid periods never yield organic conclusions. Confidence follows sample size and
out-of-sample model acceptance, not narrative certainty.
"""
from datetime import datetime, timedelta
from sqlalchemy import select
from .models import Decision, ExperimentChange
from .history import pacific_day, lag_days
from .metrics import aware

VERSION = "strategy-v4"
NO_MANIPULATION = "Keine gekauften Views, Bots, Spam, Engagement-Pods oder irreführende Titel/Thumbnails."
LABELS = {"declining": "Rückläufig", "stable": "Stabil", "growing": "Wachsend", "accelerating": "Beschleunigend",
          "breakout_candidate": "Breakout-Kandidat", "breakout": "Breakout", "paid_excluded": "Werbetraffic – keine organische Aussage",
          "insufficient_data": "Zu wenig Daten"}


MIN_VIDEOS_FOR_MODERATE = 10
GENERALIZATION_NOTE = "Zeitlich validiert auf bestehenden Videos; nicht als allgemein für neue Videos bewiesen."


def confidence(base, backtest, quality):
    """Capped by the number of independent videos: thousands of correlated days never raise it above low."""
    n_videos = base.get("n_videos", 0)
    n_rows = base.get("n_rows", 0)
    n_origins = base.get("n_origins", 0)
    accepted = bool(backtest and backtest.get("accepted"))
    cross = (backtest or {}).get("cross_video", {}) if backtest else {}
    cross_status = cross.get("status", "insufficient_data")
    # Cross-video acceptance only counts with enough independent videos, whatever a backtest record claims.
    cross_accepted = bool(cross.get("accepted")) and n_videos >= MIN_VIDEOS_FOR_MODERATE
    scope = {"temporal_within_video_validation": "accepted" if accepted else "baseline" if backtest and backtest.get("status") == "backtested" else "insufficient_data",
             "cross_video_generalization": "accepted" if cross_accepted else cross_status if n_videos >= MIN_VIDEOS_FOR_MODERATE else "insufficient_data"}
    common = {"n_rows": n_rows, "n_origins": n_origins, "n_videos": n_videos, "backtest_accepted": accepted,
              "cross_video_accepted": cross_accepted, "scope": scope, "max_level_for_n_videos": "low" if n_videos < MIN_VIDEOS_FOR_MODERATE else "moderate"}
    if base.get("status") != "ok":
        return {**common, "level": "insufficient_data", "reason": "Kanal-Baseline unter 100 organischen Tagen."}
    if n_videos < MIN_VIDEOS_FOR_MODERATE:
        return {**common, "level": "low", "reason": f"Nur {n_videos} unabhängige Videos ({n_rows} korrelierte Tageszeilen zählen nicht als unabhängige Stichprobe). "
                + GENERALIZATION_NOTE+" Beobachtungen, keine Kausalität."}
    if not accepted or not cross_accepted:
        return {**common, "level": "low", "reason": "Modelle schlagen die Baseline nicht in beiden Scopes (zeitlich und video-übergreifend). Beobachtungen, keine Kausalität."}
    return {**common, "level": "moderate", "reason": "Zeitlich und video-übergreifend validiertes Modell; weiterhin Korrelation, kein Kausalnachweis."}


def _compare(f, base, key, label, higher_is_good=True):
    value, ref = f.get(key), base.get("medians", {}).get(key, {})
    # A median of 0 or a thin sample is not a yardstick: report the signal as unavailable.
    if value is None or not ref.get("median") or (ref.get("n") or 0) < 30:
        return None
    delta = value-ref["median"]
    good = delta >= 0 if higher_is_good else delta <= 0
    return {"signal": label, "value": value, "channel_median": ref["median"], "n": ref["n"], "direction": "for" if good else "against"}


def recommend(f, regime, base, backtest, signals, experiments):
    """What happens, why it might, evidence for/against, next test, what not to touch, confidence."""
    quality = {"analytics": "available" if f else "missing", "traffic": "available" if f and f.get("traffic_total_7d") is not None else "missing",
               "retention": "available" if f and f.get("retention_avg") is not None else "missing",
               "ctr": "available" if f and f.get("ctr_7d") is not None else "missing"}
    status = regime["regime"]
    conf = confidence(base, backtest, quality)
    result = {"version": VERSION, "regime": status, "label": LABELS[status], "confidence": conf, "data_quality": quality,
              "what": regime.get("reason", ""), "why": [], "for": [], "against": [], "next": [], "do_not_change": [NO_MANIPULATION],
              "experiments": experiments, "targets": {"100000": {"status": "insufficient_data", "n_videos": base.get("n_videos", 0),
                  "reason": "Wahrscheinlichkeiten für 100k/1M erst ab 30 unabhängigen Videos mit beobachteten Ausgängen."},
                  "1000000": {"status": "insufficient_data", "n_videos": base.get("n_videos", 0)}}}
    if conf["scope"]["cross_video_generalization"] != "accepted":
        result["generalization"] = {"status": "insufficient_data", "n_videos": conf["n_videos"], "required_videos": MIN_VIDEOS_FOR_MODERATE,
                                    "note": GENERALIZATION_NOTE}
    else:
        result["generalization"] = {"status": "validated", "n_videos": conf["n_videos"], "required_videos": MIN_VIDEOS_FOR_MODERATE}
    if status == "paid_excluded":
        result["why"].append("Werbetraffic im Beobachtungsfenster; organisches Momentum ist nicht identifizierbar.")
        result["next"].append({"action": "keine organische Schlussfolgerung", "detail": "Bis 32 Tage nach dem letzten Werbetag keine Titel-/Thumbnail-Änderung als organischen Effekt interpretieren."})
        result["do_not_change"].append("Keine Strategie aus dieser Phase ableiten; Werbe- und organische Views getrennt betrachten.")
        return result
    if status == "insufficient_data" or f is None:
        result["next"].append({"action": "Daten sammeln", "detail": "Mindestens 28 Tage Analytics und 100 organische Kanaltage abwarten; Sync und Backfill prüfen."})
        return result
    checks = [_compare(f, base, "retention_avg", "Retention (Ø Wiedergaberatio)"), _compare(f, base, "pct_7d", "Ø Prozent gesehen"),
              _compare(f, base, "subscriber_conversion_7d", "Abo-Conversion"), _compare(f, base, "ctr_7d", "Thumbnail-CTR"),
              _compare(f, base, "like_rate_7d", "Like-Rate")]
    for c in checks:
        if c:
            result[c["direction"]].append(c)
    for signal in signals:
        if signal["kind"] == "standardized_ridge_coefficient" and abs(signal["value"]) >= 0.1:
            value = f.get(signal["feature"])
            if value is not None:
                result["why"].append(f"Modell-Assoziation {signal['feature']} ({signal['value']:+.2f}, standardisiert): beobachtet, nicht kausal.")
    shares = {k: f.get(f"traffic_{k}") for k in ("search", "suggested", "external", "browse", "shorts")}
    dominant = max((k for k in shares if shares[k] is not None), key=lambda k: shares[k], default=None)
    if dominant:
        result["why"].append(f"Traffic-Mix der letzten 7 bekannten Tage: {dominant} dominiert ({shares[dominant]*100:.0f} %).")
    if f.get("age_days", 0) <= 30:
        result["why"].append("Video jünger als 30 Tage: Startphase, Vergleich mit Kanal-Baseline nur eingeschränkt sinnvoll.")
    if status in ("breakout", "breakout_candidate", "accelerating"):
        result["next"] += [{"action": "Momentum schützen", "detail": "Keine Änderung an Titel, Thumbnail oder Beschreibung; Traffic-Quelle täglich beobachten."},
                           {"action": "Traffic-Source beobachten", "detail": f"Prüfen, ob {dominant or 'die Hauptquelle'} anhält; Einbruch früh erkennen."},
                           {"action": "ähnliches Content-Muster erneut testen", "detail": "Folgevideo mit vergleichbarem Muster als vorregistriertes Experiment planen."}]
        result["do_not_change"] += ["Titel", "Thumbnail", "Beschreibung/Tags", "Veröffentlichungsstatus"]
    elif status == "growing":
        result["next"] += [{"action": "nichts verändern", "detail": "Wachstum über der Kanal-Baseline; erst bei Abflachung testen."},
                           {"action": "Experiment vorbereiten", "detail": "Hypothese für das nächste Video vorregistrieren, nicht dieses ändern."}]
        result["do_not_change"] += ["Titel", "Thumbnail"]
    elif status == "declining":
        weak = [c["signal"] for c in result["against"]]
        if any("Retention" in w or "Prozent" in w for w in weak):
            result["next"].append({"action": "Retention-Schwachstelle untersuchen", "detail": "Retention-Kurve prüfen; Einstieg des nächsten Videos gezielt testen."})
        if any("CTR" in w for w in weak):
            result["next"].append({"action": "Thumbnail testen", "detail": "Nur Thumbnail ändern, Zeitpunkt protokollieren, CTR und Watchtime vergleichen."})
        elif quality["ctr"] == "missing":
            result["next"].append({"action": "Titel testen", "detail": "Ohne CTR-Daten nur Titel ändern und als Experiment registrieren; Thumbnail konstant halten."})
        result["next"].append({"action": "Experiment starten", "detail": "Genau eine Dimension ändern und im Experiment Memory vorregistrieren."})
        result["do_not_change"].append("Nicht Titel und Thumbnail gleichzeitig ändern.")
    else:
        result["next"] += [{"action": "nichts verändern", "detail": "Stabil innerhalb der Kanal-Baseline; Ressourcen auf das nächste Video."},
                           {"action": "Traffic-Source beobachten", "detail": "Anteilsverschiebungen ≥ 10 Prozentpunkte als Hypothese erfassen."}]
    if not result["for"] and not result["against"]:
        result["why"].append("Qualitätssignale ohne belastbare Kanalmediane (n < 30) – keine Für/Wider-Bewertung.")
    return result


def experiment_context(session, video_id, history, now):
    """Before/after windows around reported changes; effect only when data allows, contradictions flagged."""
    rows = list(session.scalars(select(Decision).where(Decision.video_id == video_id,
        Decision.status.in_(["registered", "evaluated"])).order_by(Decision.created_at)))
    known_end = pacific_day(now)-timedelta(days=lag_days())
    out, by_key = [], {}
    for d in rows:
        changes = list(session.scalars(select(ExperimentChange).where(ExperimentChange.decision_id == d.id).order_by(ExperimentChange.applied_at)))
        applied = aware(changes[0].applied_at) if changes else datetime.fromisoformat(d.measurement["origin_at"]) if d.measurement else None
        entry = {"decision_id": d.id, "status": d.status, "hypothesis": d.hypothesis, "strategy_key": d.strategy_key,
                 "applied_at": applied.isoformat() if applied else None, "changes": len(changes), "effect": None,
                 "effect_status": "insufficient_data"}
        if applied and history is not None:
            day = pacific_day(applied)
            before = [day-timedelta(days=i) for i in range(7, 0, -1)]
            after = [day+timedelta(days=i) for i in range(1, 8)]
            entry.update(before_window=[str(before[0]), str(before[-1])], after_window=[str(after[0]), str(after[-1])])
            if after[-1] > known_end:
                entry["effect_status"] = "after_window_not_yet_observed"
            elif history.paid_views(before[0], after[-1]) > 0:
                entry["effect_status"] = "paid_excluded"
            elif history.first_day is None or before[0] < history.first_day:
                entry["effect_status"] = "before_window_missing"
            else:
                b = sum(history.views(x) for x in before)/7
                a = sum(history.views(x) for x in after)/7
                entry["effect"] = {"before_mean_daily": b, "after_mean_daily": a, "relative_change": (a-b)/max(1.0, b)}
                entry["effect_status"] = "observed_change_not_causal"
        if d.actual_result:
            entry["memory_result"] = {"relative_deviation": d.actual_result.get("relative_deviation"),
                                      "control_adjusted_residual": d.actual_result.get("control_adjusted_residual")}
            by_key.setdefault(d.strategy_key, []).append(d.actual_result.get("relative_deviation"))
        out.append(entry)
    for entry in out:
        outcomes = [x for x in by_key.get(entry["strategy_key"], []) if x is not None]
        entry["conflicting_evidence"] = len(outcomes) >= 2 and any(x > 0 for x in outcomes) and any(x <= 0 for x in outcomes)
    return out
