"""V5 Active Organic Growth Engine: rank, decide, protect, measure – never write to YouTube.

Scores are relative priorities on this channel (0–100), assembled from components
that are actually available; missing inputs are listed, never imputed. States and
actions follow V4 regimes and video-balanced channel baselines. Every action is a
recommendation with success and stop criteria and is later scored against observed
analytics. Confidence inherits the V4 caps: with few videos it never exceeds "low".
"""
from datetime import timedelta
from math import tanh
from sqlalchemy import select
from .models import (GrowthAssessment, GrowthScore, GrowthAction, GrowthPlan, AnalyticsForecast, Decision, Video, utcnow)
from .backfill import upsert
from .history import pacific_day, lag_days, features_at, paid_profile, PAID_WINDOW_DAYS
from .metrics import aware
from .strategy import confidence as v4_confidence, NO_MANIPULATION, GENERALIZATION_NOTE
from .discovery import best_for_video as external_opportunity

VERSION = "growth-v5"
STATES = ["protect_momentum", "scale_opportunity", "needs_packaging_test", "needs_retention_analysis", "needs_discovery",
          "revival_candidate", "observe", "paid_cooldown", "paid_excluded", "insufficient_data"]
PAID_LABELS = {"organic": "Aktuell organisch", "organic_with_paid_history": "Aktuell organisch – historisch Werbung vorhanden",
               "paid_cooldown": "Paid-Cooldown", "paid_excluded": "Aktuell Paid beeinflusst"}
ACTIONS = ["protect_no_change", "test_title", "test_thumbnail", "test_title_thumbnail", "investigate_retention",
           "improve_discovery", "cross_promote", "create_followup_content", "observe",
           "target_search_opportunity", "target_suggested_cluster", "packaging_for_audience", "revive_existing_video"]
EXTERNAL_MIN_SCORE = 60  # Relative external score needed before an external opportunity may drive the action.
EXTERNAL_STATES = ("observe", "needs_discovery", "needs_packaging_test", "scale_opportunity", "revival_candidate")
GAP_ACTIONS = {"existing_video_opportunity": "target_search_opportunity", "search_opportunity": "target_search_opportunity",
               "suggested_opportunity": "target_suggested_cluster", "packaging_opportunity": "packaging_for_audience",
               "followup_content_opportunity": "create_followup_content"}
PROTECT_REGIMES = ("breakout", "breakout_candidate", "accelerating")
DISCOVERY_KEYS = ("traffic_search", "traffic_suggested", "traffic_browse")
SCORE_NOTE = "Relativer Priorisierungswert für diesen Kanal (0–100), keine Wahrscheinlichkeit."
OBJECTIVES = {"protect_no_change": "Discovery", "test_title": "Viewer", "test_thumbnail": "Viewer", "test_title_thumbnail": "Viewer",
              "investigate_retention": "Watchtime", "improve_discovery": "Discovery", "cross_promote": "Discovery",
              "create_followup_content": "Subscriber", "observe": "Watchtime", "target_search_opportunity": "Viewer",
              "target_suggested_cluster": "Discovery", "packaging_for_audience": "Viewer", "revive_existing_video": "Viewer"}
WINDOWS = {"protect_no_change": 7, "test_title": 14, "test_thumbnail": 14, "test_title_thumbnail": 14, "investigate_retention": 14,
           "improve_discovery": 14, "cross_promote": 14, "create_followup_content": 30, "observe": 7,
           "target_search_opportunity": 28, "target_suggested_cluster": 28, "packaging_for_audience": 14, "revive_existing_video": 28}
TARGETS = {"protect_no_change": "views_7d", "test_title": "views_7d", "test_thumbnail": "ctr_or_views", "test_title_thumbnail": "ctr_or_views",
           "investigate_retention": "watch_minutes_7d", "improve_discovery": "discovery_views_7d", "cross_promote": "views_7d",
           "create_followup_content": "subscribers_7d", "observe": "views_7d", "target_search_opportunity": "discovery_views_7d",
           "target_suggested_cluster": "discovery_views_7d", "packaging_for_audience": "ctr_or_views", "revive_existing_video": "views_7d"}
DO_NOT_CHANGE = {"protect_no_change": ["Titel", "Thumbnail", "Beschreibung/Tags", "Sichtbarkeit", "Endscreens des Videos"],
                 "test_title": ["Thumbnail", "Beschreibung", "Kapitel"], "test_thumbnail": ["Titel", "Beschreibung", "Kapitel"],
                 "test_title_thumbnail": ["Beschreibung", "Videoinhalt", "Sichtbarkeit"],
                 "investigate_retention": ["Titel", "Thumbnail"], "improve_discovery": ["Titel", "Thumbnail", "Videoinhalt"],
                 "cross_promote": ["Titel und Thumbnail des beworbenen Videos"], "create_followup_content": ["Bestehendes Video"],
                 "observe": ["Alles – zuerst messen"], "target_search_opportunity": ["Thumbnail", "Videoinhalt"],
                 "target_suggested_cluster": ["Titel", "Thumbnail", "Videoinhalt"], "packaging_for_audience": ["Videoinhalt", "Sichtbarkeit"],
                 "revive_existing_video": ["Videoinhalt", "Sichtbarkeit"]}
MIN_TRACK_RECORD = 3


# ----------------------------------------------------------------------------- components
def _rank_signal(value, quantiles, low="q25", high="q75", cap="q95"):
    """Position of a value inside the channel's video-balanced quantile band, squashed to [-1, 1]."""
    if value is None or not quantiles:
        return None
    center = quantiles.get("q50")
    width = max(1e-9, (quantiles.get(cap, quantiles.get(high)) or 0)-(quantiles.get("q05", quantiles.get(low)) or 0))
    return tanh(2*(value-center)/width)


def _median_signal(f, base, key, higher_is_good=True):
    ref = base.get("medians", {}).get(key, {})
    value = f.get(key)
    if value is None or ref.get("median") is None or ref.get("n", 0) < 30:
        return None, ref
    center = ref["median"]
    signal = tanh((value-center)/center) if center else (0.5 if value > 0 else 0.0)
    return (signal if higher_is_good else -signal), ref


def _component(name, signal, weight, value=None, reference=None, note=None):
    return {"name": name, "signal": None if signal is None else round(float(signal), 4), "weight": weight, "value": value,
            "reference": reference, "available": signal is not None, "note": note}


def _score(components):
    usable = [c for c in components if c["available"]]
    if not usable:
        return None
    total = sum(c["weight"] for c in usable)
    return round(50+50*sum(c["weight"]*c["signal"] for c in usable)/total, 1)


def discovery_share(f):
    parts = [f.get(k) for k in DISCOVERY_KEYS]
    return None if all(p is None for p in parts) else sum(p or 0 for p in parts)


def live_momentum(session, video_id):
    """Latest V2 snapshot assessment (real observation times) as a live cross-check."""
    row = session.scalar(select(GrowthAssessment).where(GrowthAssessment.video_id == video_id).order_by(GrowthAssessment.origin_at.desc()))
    if row is None:
        return None
    return {"score": row.assessment.get("score"), "regime": row.assessment.get("regime"), "velocity": row.features.get("velocity"),
            "acceleration": row.features.get("acceleration"), "origin_at": aware(row.origin_at).isoformat()}


def forecast_uplift(forecasts):
    weekly = next((p for p in forecasts if p["horizon_hours"] == 168), None)
    if not weekly or not weekly.get("baseline_views"):
        return None, None, weekly
    uplift = weekly["predicted_views"]/weekly["baseline_views"]-1
    width = ((weekly["upper_views"]-weekly["lower_views"])/max(1.0, weekly["predicted_views"])) if weekly.get("lower_views") is not None else None
    return uplift, width, weekly


def paid_state(f):
    """Graded paid status of the current window; historical ads alone never block scoring."""
    profile = (f or {}).get("paid")
    if profile is None:
        # Features without a profile (legacy rows): any paid views in the live windows count as current contamination.
        contaminated = ((f or {}).get("paid_views_32d") or 0) > 0 or ((f or {}).get("paid_views_lag_gap") or 0) > 0
        profile = {"status": "paid_excluded" if contaminated else "organic", "paid_days_total": 0}
    return profile.get("status", "organic"), profile


def scores(f, regime, base, forecasts, momentum, peak, external=None):
    """Opportunity, viewer-acquisition and subscriber-opportunity scores with explained components."""
    status = regime["regime"]
    external_signal = None if not external or external.get("score") is None else (external["score"]-50)/50
    fit = (external or {}).get("scores", {}).get("subscriber_fit_score")
    subscriber_fit_signal = None if fit is None else (fit-50)/50
    if status == "paid_excluded":
        paid, profile = paid_state(f)
        reason = ("Werbetraffic im aktuellen 7-Tage-Fenster oder in der Analytics-Lücke – kein organischer Prioritätswert." if paid == "paid_excluded"
                  else f"Paid-Cooldown: letzter Werbetag {profile.get('last_paid_day')}, sauberes Fenster {profile.get('clean_days')} von "
                       f"{profile.get('required_clean_days', PAID_WINDOW_DAYS)} Tagen – Scores folgen automatisch nach {profile.get('days_until_clean')} weiteren sauberen Tagen.")
        blocked = {"score": None, "components": [], "reason": reason, "note": SCORE_NOTE, "paid": profile}
        return {"opportunity": blocked, "viewer": blocked, "subscriber": blocked}
    if f is None or base.get("status") != "ok":
        blocked = {"score": None, "components": [], "reason": "Zu wenig Historie oder keine belastbare Kanal-Baseline.", "note": SCORE_NOTE}
        return {"opportunity": blocked, "viewer": blocked, "subscriber": blocked}
    rq, aq = base.get("ratio_7_28", {}), base.get("accel_7d", {})
    pace = _rank_signal(f.get("ratio_7_28"), rq)
    accel = _rank_signal(f.get("accel_7d"), aq)
    regime_signal = {"breakout": 1.0, "breakout_candidate": .8, "accelerating": .6, "growing": .4, "stable": 0.0, "declining": -.5}.get(status)
    live = None if not momentum or momentum.get("score") is None else (momentum["score"]-50)/50
    retention, ret_ref = _median_signal(f, base, "retention_avg")
    pct, pct_ref = _median_signal(f, base, "pct_7d")
    conversion, conv_ref = _median_signal(f, base, "subscriber_conversion_7d")
    ctr, ctr_ref = _median_signal(f, base, "ctr_7d")
    share = discovery_share(f)
    discovery = None
    if share is not None:
        ref = [base.get("medians", {}).get(k, {}).get("median") for k in DISCOVERY_KEYS]
        if all(r is not None for r in ref) and all(base["medians"][k].get("n", 0) >= 30 for k in DISCOVERY_KEYS):
            center = sum(ref)
            discovery = tanh((share-center)/center) if center else None
    uplift, width, weekly = forecast_uplift(forecasts)
    forecast = None if uplift is None else tanh(uplift)
    uncertainty = None if width is None else -min(1.0, width/4)
    age = f.get("age_days")
    age_signal = None if age is None else (.3 if age <= 90 else 0.0 if age <= 365 else -.1)
    peak_signal = None
    if peak and peak.get("peak_velocity") and f.get("velocity_7d") is not None:
        peak_signal = -tanh(1-f["velocity_7d"]/peak["peak_velocity"])  # far below own peak = revival headroom, mild
    watch_per_sub = None
    if f.get("subs_gained_7d") and f.get("watch_minutes_7d"):
        watch_per_sub = f["watch_minutes_7d"]/f["subs_gained_7d"]
    opportunity = [
        _component("Tempo 7d vs 28d (Kanalquantile)", pace, 3, f.get("ratio_7_28"), rq.get("q50")),
        _component("Wochenbeschleunigung", accel, 2, f.get("accel_7d"), aq.get("q50")),
        _component("Regime V4", regime_signal, 2, status),
        _component("Live-Momentum (Snapshots, V2)", live, 1, momentum.get("score") if momentum else None, 50),
        _component("Retention vs Kanalmedian", retention, 1.5, f.get("retention_avg"), ret_ref.get("median")),
        _component("Ø Prozent gesehen vs Median", pct, 1, f.get("pct_7d"), pct_ref.get("median")),
        _component("Abo-Conversion vs Median", conversion, 1, f.get("subscriber_conversion_7d"), conv_ref.get("median")),
        _component("Thumbnail-CTR vs Median", ctr, 1, f.get("ctr_7d"), ctr_ref.get("median")),
        _component("Discovery-Anteil (Search+Suggested+Browse)", discovery, 1.5, share),
        _component("V4-Prognose 7d vs Baseline", forecast, 1.5, uplift),
        _component("Prognose-Unsicherheit (Intervallbreite)", uncertainty, 1, width, note="nur bei empirischem Intervall"),
        _component("Videoalter", age_signal, .5, age),
        _component("Externe Audience-Chance (V6)", external_signal, 1.5, external.get("score") if external else None,
                   note=("Nachfrage: "+("eigene Analytics" if external.get("demand_source") == "own_analytics" else "öffentlicher Proxy")) if external else "keine externe Chance erkannt"),
    ]
    viewer = [
        _component("Discovery-Anteil (Search+Suggested+Browse)", discovery, 3, share),
        _component("Tempo 7d vs 28d (Kanalquantile)", pace, 2, f.get("ratio_7_28"), rq.get("q50")),
        _component("Wochenbeschleunigung", accel, 1.5, f.get("accel_7d"), aq.get("q50")),
        _component("Thumbnail-CTR vs Median", ctr, 2, f.get("ctr_7d"), ctr_ref.get("median")),
        _component("V4-Prognose 7d vs Baseline", forecast, 1.5, uplift),
        _component("Regime V4", regime_signal, 1.5, status),
        _component("Abstand zum eigenen Peak", peak_signal, 1, f.get("velocity_7d"), peak.get("peak_velocity") if peak else None),
        _component("Videoalter", age_signal, .5, age),
        _component("Externe Audience-Chance (V6)", external_signal, 2, external.get("score") if external else None,
                   note=("Nachfrage: "+("eigene Analytics" if external.get("demand_source") == "own_analytics" else "öffentlicher Proxy")) if external else "keine externe Chance erkannt"),
    ]
    subscriber = [
        _component("Abo-Conversion vs Median", conversion, 3, f.get("subscriber_conversion_7d"), conv_ref.get("median")),
        _component("Watchtime je gewonnenem Abo (niedriger = effizienter)", None if watch_per_sub is None else -tanh((watch_per_sub-60)/60), 1.5, watch_per_sub, 60,
                   note="Referenz 60 min/Abo ist eine Konvention, kein Kanalwert"),
        _component("Netto-Abos 7d", None if f.get("subs_net_7d") is None else tanh(f["subs_net_7d"]/10), 1, f.get("subs_net_7d")),
        _component("Tempo 7d vs 28d (mehr Reichweite = mehr Abos)", pace, 1.5, f.get("ratio_7_28"), rq.get("q50")),
        _component("Retention vs Kanalmedian", retention, 1, f.get("retention_avg"), ret_ref.get("median")),
        _component("Discovery-Anteil (neue Zuschauer)", discovery, 1, share),
        _component("Regime V4 (organische Wachstumsphase)", regime_signal, 1, status),
        _component("Videoalter", age_signal, .5, age),
        _component("Subscriber-Fit der externen Chance (V6)", subscriber_fit_signal, 1, (external or {}).get("scores", {}).get("subscriber_fit_score")),
    ]
    def pack(components, reason):
        return {"score": _score(components), "components": components, "missing": [c["name"] for c in components if not c["available"]],
                "reason": reason, "note": SCORE_NOTE}
    return {"opportunity": pack(opportunity, "Kombination aus Tempo, Regime, Qualität, Discovery und V4-Prognose."),
            "viewer": pack(viewer, "Potenzial, zusätzliche organische Zuschauer zu erreichen (Discovery, Tempo, CTR)."),
            "subscriber": pack(subscriber, "Potenzial, aktuelle Zuschauer in Abonnenten zu verwandeln (Conversion, Effizienz).")}


# ----------------------------------------------------------------------------- revival & states
def historical_peak(history, today):
    """Organic 7-day peak from ad-free weeks outside any paid spillover window; the paid peak is kept apart for audit.

    A week counts as organic only if no advertising day lies inside the week or within
    PAID_WINDOW_DAYS before it (post-campaign spillover is not organic evidence).
    """
    if history.first_day is None:
        return None
    paid_days = history.paid_days()
    best, best_day, paid_best, paid_best_day = 0.0, None, 0.0, None
    day = history.first_day+timedelta(days=6)
    end = min(history.last_day, today)
    while day <= end:
        start = day-timedelta(days=6)
        pace = sum(history.views(start+timedelta(days=i)) for i in range(7))/7
        tainted = any(start-timedelta(days=PAID_WINDOW_DAYS) <= p <= day for p in paid_days)
        if tainted:
            if pace > paid_best:
                paid_best, paid_best_day = pace, day
        elif pace > best:
            best, best_day = pace, day
        day += timedelta(days=7)
    return {"peak_velocity": best, "peak_week_end": str(best_day) if best_day else None, "basis": "organic_weeks_only",
            "paid_peak_velocity": paid_best if paid_days else None, "paid_peak_week_end": str(paid_best_day) if paid_best_day else None,
            "paid_days_total": len(paid_days), "spillover_days_excluded": PAID_WINDOW_DAYS}


def revival(f, regime, base, peak):
    """Old video that could grow again: at least two independent revival signals."""
    if f is None or regime["regime"] in ("paid_excluded", "insufficient_data") or f.get("age_days", 0) < 180:
        return {"candidate": False, "signals": [], "reason": "Nur Videos ab 180 Tagen mit sauberem aktuellem Fenster werden auf Revival geprüft."}
    signals = []
    aq = base.get("accel_7d", {})
    if aq and (f.get("accel_7d", 0) >= aq.get("q75", 1e9) or f.get("days_above_28d_last3", 0) >= 2):
        signals.append("erneute Beschleunigung über Kanal-q75")
    med = base.get("medians", {})
    def above(key):
        ref = med.get(key, {})
        return ref.get("median") is not None and ref.get("n", 0) >= 30 and f.get(key) is not None and f[key] > ref["median"]
    def below(key):
        ref = med.get(key, {})
        return ref.get("median") is not None and ref.get("n", 0) >= 30 and f.get(key) is not None and f[key] < ref["median"]
    if above("traffic_search"):
        signals.append("Search-Anteil über Kanalmedian")
    if above("traffic_suggested") or above("traffic_browse"):
        signals.append("Suggested/Browse-Anteil über Kanalmedian")
    far_below_peak = peak and peak.get("peak_velocity") and f.get("velocity_7d") is not None and f["velocity_7d"] < 0.2*peak["peak_velocity"]
    if above("retention_avg") and (far_below_peak or below("traffic_suggested")):
        signals.append("gute Retention trotz schwacher Distribution")
    if above("subscriber_conversion_7d") and (far_below_peak or f.get("ratio_7_28", 1) < 1):
        signals.append("gute Abo-Conversion trotz niedriger Views")
    if below("ctr_7d") and (above("retention_avg") or above("pct_7d")):
        signals.append("Packaging-/CTR-Schwäche bei guten Qualitätswerten")
    paid, profile = paid_state(f)
    return {"candidate": len(signals) >= 2, "signals": signals, "far_below_peak": bool(far_below_peak), "peak": peak,
            "paid_status": paid, "post_paid": paid == "organic_with_paid_history",
            "reason": (f"{len(signals)} von 6 Revival-Signalen" if signals else "Keine Revival-Signale.")
                      +(f" – organisches Revival nach Werbung (letzter Werbetag {profile.get('last_paid_day')}, {profile.get('clean_days')} saubere Tage; Peak nur aus werbefreien Wochen)." if paid == "organic_with_paid_history" else "")}


def state_of(f, regime, base, rev):
    status = regime["regime"]
    if status == "paid_excluded":
        paid, _ = paid_state(f)
        return "paid_excluded" if paid == "paid_excluded" else "paid_cooldown"
    if status == "insufficient_data" or f is None:
        return "insufficient_data"
    if status in PROTECT_REGIMES:
        return "protect_momentum"
    if status == "growing":
        return "scale_opportunity"
    if rev.get("candidate"):
        return "revival_candidate"
    med = base.get("medians", {})
    def below(key):
        ref = med.get(key, {})
        return ref.get("median") is not None and ref.get("n", 0) >= 30 and f.get(key) is not None and f[key] < ref["median"]
    if status == "declining" and (below("retention_avg") or below("pct_7d")):
        return "needs_retention_analysis"
    if status == "declining" and (below("ctr_7d") or f.get("ctr_7d") is None):
        return "needs_packaging_test"
    share = discovery_share(f)
    ref = [med.get(k, {}) for k in DISCOVERY_KEYS]
    if share is not None and all(r.get("median") is not None and r.get("n", 0) >= 30 for r in ref) and share < sum(r["median"] for r in ref)*0.8:
        return "needs_discovery"
    return "observe"


# ----------------------------------------------------------------------------- actions
def track_record(session):
    """Observed outcomes per action type; descriptive, not a causal effect estimate."""
    record = {}
    for row in session.scalars(select(GrowthAction).where(GrowthAction.status == "evaluated")):
        entry = record.setdefault(row.action, {"positive": 0, "negative": 0, "neutral": 0, "inconclusive": 0, "n": 0})
        entry[row.outcome] = entry.get(row.outcome, 0)+1
        entry["n"] += 1
    for entry in record.values():
        entry["net_negative"] = entry["n"] >= MIN_TRACK_RECORD and entry["negative"] > entry["positive"]
    return record


def choose_action(state, f, rev, base, experiments, record, external=None):
    """Exactly one prioritised action. Winners are protected first; running experiments are measured, not stacked.
    An external opportunity (V6) may steer the action only for organic, non-protected states."""
    running = [e for e in experiments if e.get("status") == "registered"]
    med = base.get("medians", {})
    def below(key):
        ref = med.get(key, {})
        return ref.get("median") is not None and ref.get("n", 0) >= 30 and f is not None and f.get(key) is not None and f[key] < ref["median"]
    notes = []
    if state == "protect_momentum":
        action = "protect_no_change"
    elif running:
        action, notes = "observe", [f"Experiment #{running[0]['decision_id']} läuft – erst messen, keine weitere Änderung stapeln."]
    elif external and (external.get("score") or 0) >= EXTERNAL_MIN_SCORE and state in EXTERNAL_STATES and external.get("gap") in GAP_ACTIONS:
        action = "revive_existing_video" if state == "revival_candidate" and external["gap"] != "packaging_opportunity" else GAP_ACTIONS[external["gap"]]
        notes = [f"Externe Chance ({external['kind']}: {external['key']}, Score {external['score']}, Nachfrage: "
                 f"{'eigene Analytics' if external.get('demand_source') == 'own_analytics' else 'öffentlicher Proxy'}) lenkt die Aktion."]
    elif state == "scale_opportunity":
        action = "cross_promote"
    elif state == "needs_retention_analysis":
        action = "investigate_retention"
    elif state == "needs_packaging_test":
        action = "test_thumbnail" if f.get("ctr_7d") is not None else "test_title"
    elif state == "needs_discovery":
        action = "improve_discovery"
    elif state == "revival_candidate":
        signals = rev.get("signals", [])
        if any("Beschleunigung" in s for s in signals):
            action, notes = "protect_no_change", ["Revival mit erneuter Beschleunigung: erst schützen, nicht umbauen."]
        elif any("Packaging" in s for s in signals):
            action = "test_title_thumbnail" if f.get("age_days", 0) > 365 and below("ctr_7d") else "test_thumbnail"
        elif any("Distribution" in s for s in signals) or any("Search" in s for s in signals):
            action = "improve_discovery"
        else:
            action = "cross_promote"
    elif state == "paid_cooldown":
        profile = (f or {}).get("paid") or {}
        action, notes = "observe", [f"Paid-Cooldown: noch {profile.get('days_until_clean')} saubere Tage bis zur organischen Bewertung; keine organische Schlussfolgerung."]
    elif state in ("paid_excluded", "insufficient_data"):
        action, notes = "observe", ["Keine organische Entscheidung möglich (aktuell Werbung oder zu wenig Daten)."]
    else:
        action = "observe"
    entry = record.get(action, {})
    if entry.get("net_negative") and action != "protect_no_change":
        notes.append(f"Beobachtete Bilanz für {action}: {entry['negative']} negativ / {entry['positive']} positiv – herabgestuft auf Beobachten (kein Kausalnachweis).")
        action = "observe"
    return action, notes


def action_details(action, state, f, regime, base, scoreboard, rev, momentum, conf, notes, experiments, external=None):
    """Reason, signals, counterarguments, target metric, window, success and stop criteria."""
    signals = []
    if external:
        signals.append({"signal": "external_audience_score_v6", "value": external.get("score")})
    if f:
        signals += [{"signal": "ratio_7_28", "value": f.get("ratio_7_28")}, {"signal": "accel_7d", "value": f.get("accel_7d")},
                    {"signal": "retention_avg", "value": f.get("retention_avg")}, {"signal": "ctr_7d", "value": f.get("ctr_7d")},
                    {"signal": "subscriber_conversion_7d", "value": f.get("subscriber_conversion_7d")}, {"signal": "discovery_share", "value": discovery_share(f)}]
    if momentum:
        signals.append({"signal": "live_momentum_v2", "value": momentum.get("score")})
    against = []
    if conf["level"] != "moderate":
        against.append("Confidence höchstens „low“: nur 3 Videos, Muster nicht für neue Videos bewiesen.")
    if f and f.get("ctr_7d") is None and action in ("test_title", "test_thumbnail", "test_title_thumbnail"):
        against.append("Ohne CTR-Historie ist der Packaging-Effekt nur über Views messbar (verwechselbar mit Saison/Distribution).")
    if f and f.get("retention_avg") is None:
        against.append("Retention des laufenden Monats noch unbekannt; Qualitätssignal fehlt.")
    if regime["regime"] == "stable":
        against.append("Stabiles Regime: Veränderungen können ebenso gut Rauschen wie Wirkung zeigen.")
    if rev.get("candidate") and action != "protect_no_change":
        against.append("Revival-Signale sind korrelativ; alte Videos reagieren träge auf Packaging.")
    reason = {
        "protect_no_change": "Organische Beschleunigung/Breakout-Signale: bestehendes Momentum nicht durch Experimente gefährden.",
        "test_title": "Rückläufig ohne CTR-Daten: Titel ist die messbare Ein-Dimension-Änderung.",
        "test_thumbnail": "CTR unter Kanalmedian bei akzeptabler Qualität: Thumbnail als einzelne Änderung testen.",
        "test_title_thumbnail": "Altes Video mit Packaging-Schwäche und guten Qualitätswerten: Paket neu positionieren.",
        "investigate_retention": "Retention/Ø Prozent unter Kanalmedian: Abbruchstellen finden, bevor am Packaging gedreht wird.",
        "improve_discovery": "Discovery-Anteil unter Kanalmedian: Playlists, Endscreens, Kapitel, Beschreibung mit Suchbezug prüfen.",
        "cross_promote": "Wachstum über Baseline: bestehendes Publikum über Endscreens/Playlists/Community auf dieses Video lenken.",
        "create_followup_content": "Muster funktioniert: verwandtes Folgevideo als vorregistriertes Experiment.",
        "observe": "Keine belastbare Änderung ableitbar oder Messung läuft: beobachten und Daten sammeln.",
        "target_search_opportunity": f"Reale/proxy Suchnachfrage „{(external or {}).get('key', '')}“ passt zum Video: Titel-/Beschreibungswortlaut auf diese Suchintention ausrichten (ohne Clickbait), Kapitel und Playlist-Kontext ergänzen.",
        "target_suggested_cluster": f"Nachbarvideo/-cluster „{(external or {}).get('audience', '')}“ erreicht eine passende Audience: Endscreens, Playlists und Beschreibung auf diesen Themenkontext ausrichten, damit die Empfehlung neben diesen Videos wahrscheinlicher wird.",
        "packaging_for_audience": f"Das Video passt zu „{(external or {}).get('key', '')}“, aber Titel/Thumbnail sprechen diese Audience nicht an: Packaging für diese Zielgruppe testen (eine Dimension).",
        "revive_existing_video": f"Altes Video mit Revival-Signalen und externer Chance „{(external or {}).get('key', '')}“: gezielt für diese Audience reaktivieren (Playlist, Endscreens, Community-Post, ggf. Titel).",
    }[action]
    window = WINDOWS[action]
    target = TARGETS[action]
    success = {
        "protect_no_change": "7-Tage-Views bleiben ≥ 85 % des Vorher-Fensters; Regime bleibt wachsend/beschleunigend.",
        "observe": "Datenlage vollständig (Retention, Traffic, ggf. CTR) und Regime unverändert oder besser.",
    }.get(action, f"{target} im Nachher-Fenster ≥ +15 % gegenüber Vorher-Fenster und über der typischen Wochenschwankung des Kanals; ohne Werbetraffic.")
    stop = ("Nicht anwendbar – keine Änderung." if action in ("protect_no_change", "observe") else
            "Views oder Watchtime im Nachher-Fenster ≥ 15 % unter Vorher, oder Retention fällt unter Kanalmedian: Änderung zurücknehmen und als negativ protokollieren.")
    template = None
    if action in ("test_title", "test_thumbnail", "test_title_thumbnail", "create_followup_content", "improve_discovery", "cross_promote",
                  "target_search_opportunity", "target_suggested_cluster", "packaging_for_audience", "revive_existing_video"):
        template = {"hypothesis": f"{action}: {reason}", "horizon_hours": 168 if window <= 14 else 720,
                    "design": "observational", "note": "Vor der Änderung im Experiment Memory registrieren; Zeitpunkt protokollieren."}
    return {"action": action, "state": state, "reason": reason, "notes": notes, "signals": signals, "against": against,
            "confidence": conf, "target_metric": target, "window_days": window, "success_criterion": success, "stop_criterion": stop,
            "do_not_change": DO_NOT_CHANGE[action]+[NO_MANIPULATION], "objective": OBJECTIVES[action],
            "experiment_template": template, "linked_decision_ids": [e["decision_id"] for e in experiments],
            "audience": {"target": external.get("audience"), "opportunity": external.get("key"), "kind": external.get("kind"), "gap": external.get("gap"),
                         "score": external.get("score"), "demand_source": external.get("demand_source"), "shared_tokens": external.get("shared_tokens"),
                         "evidence": {k: v for k, v in (external.get("evidence") or {}).items() if k in ("own_search_views_90d", "own_suggested_views_90d", "probe", "channel", "views", "uncertainty", "missing")}}
                        if external else None,
            "read_only": "Empfehlung – das System ändert nichts auf YouTube.", "generalization": GENERALIZATION_NOTE}


# ----------------------------------------------------------------------------- feedback
def _window_metrics(history, start, end):
    days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    rows = [history.daily[d] for d in days if d in history.daily]
    views = sum(history.views(d) for d in days)
    totals = {}
    for d in days:
        for source, (v, _) in history.traffic.get(d, {}).items():
            totals[source] = totals.get(source, 0)+v
    traffic_total = sum(totals.values())
    discovery = sum(v for s, v in totals.items() if s in ("YT_SEARCH", "RELATED_VIDEO", "YT_CHANNEL", "SUBSCRIBER", "NOTIFICATION", "END_SCREEN", "PLAYLIST", "YT_PLAYLIST_PAGE", "YT_OTHER_PAGE"))
    subs = sum(r.subscribers_gained for r in rows)
    reach = [history.reach[d] for d in days if d in history.reach and history.reach[d].ctr is not None]
    impressions = sum(r.impressions for r in reach)
    return {"days": len(days), "observed_days": len(rows), "views": views, "velocity": views/len(days), "watch_minutes": sum(r.watch_minutes for r in rows),
            "subscribers": subs, "subscriber_conversion": subs/views if views else None,
            "discovery_views": discovery if traffic_total else None, "discovery_share": discovery/traffic_total if traffic_total else None,
            "ctr": sum(r.impressions*r.ctr for r in reach)/impressions if impressions else None,
            "avg_percentage": (sum(r.average_percentage*r.views for r in rows)/views) if views and rows else None,
            "paid_views": history.paid_views(start, end)}


def evaluate_actions(session, now, by_id, base):
    """Score pending actions once their window is fully observed; paid or missing data stay inconclusive."""
    today = pacific_day(now)
    known_end = today-timedelta(days=lag_days())
    evaluated = 0
    for row in session.scalars(select(GrowthAction).where(GrowthAction.status == "pending")):
        after_end = row.created_day+timedelta(days=row.window_days)
        if after_end > known_end:
            continue
        history = by_id.get(row.video_id)
        if history is None:
            continue
        before = _window_metrics(history, row.created_day-timedelta(days=row.window_days), row.created_day-timedelta(days=1))
        after = _window_metrics(history, row.created_day+timedelta(days=1), after_end)
        regime_after = None
        f_after = features_at(history, after_end+timedelta(days=lag_days()))
        if f_after is not None:
            from .regimes import classify
            regime_after = classify(f_after, base)["regime"]
        outcome, detail = _outcome(row, before, after, base)
        linked = list(session.scalars(select(Decision.id).where(Decision.video_id == row.video_id, Decision.status.in_(["registered", "evaluated"]),
            Decision.created_at >= aware(row.created_at)-timedelta(days=1))))
        row.status, row.outcome, row.evaluated_at = "evaluated", outcome, now
        row.evaluation = {"before": before, "after": after, "regime_after": regime_after, "detail": detail,
                          "decision_ids": linked, "note": "Beobachtete Veränderung, keine Kausalwirkung; Saison, Distribution und Algorithmus sind nicht kontrolliert."}
        evaluated += 1
    return evaluated


def _outcome(row, before, after, base):
    if before["paid_views"] > 0 or after["paid_views"] > 0:
        return "inconclusive", {"reason": "Werbetraffic im Vorher- oder Nachher-Fenster."}
    if before["observed_days"] == 0 or after["observed_days"] == 0:
        return "inconclusive", {"reason": "Analytics-Tage fehlen."}
    target = row.target_metric
    if target == "ctr_or_views":
        key, b, a = ("ctr", before["ctr"], after["ctr"]) if before["ctr"] is not None and after["ctr"] is not None else ("views", before["views"], after["views"])
    elif target == "watch_minutes_7d":
        key, b, a = "watch_minutes", before["watch_minutes"], after["watch_minutes"]
    elif target == "discovery_views_7d":
        key, b, a = "discovery_views", before["discovery_views"], after["discovery_views"]
    elif target == "subscribers_7d":
        key, b, a = "subscribers", before["subscribers"], after["subscribers"]
    else:
        key, b, a = "views", before["views"], after["views"]
    if b is None or a is None:
        return "inconclusive", {"reason": f"Zielmetrik {key} nicht verfügbar."}
    change = (a-b)/max(1.0, b)
    noise = abs(base.get("accel_7d", {}).get("q75", 0.15)) if base else 0.15
    detail = {"metric": key, "before": b, "after": a, "relative_change": change, "channel_weekly_noise_q75": noise}
    if row.action == "protect_no_change":
        return ("positive" if change >= -0.15 else "negative"), detail
    if change >= max(0.15, noise):
        return "positive", detail
    if change <= -0.15:
        return "negative", detail
    return "neutral", detail


# ----------------------------------------------------------------------------- orchestration
def run(session, now, contexts, base, budget=None):
    """One growth pass: evaluate old actions, score, decide and write today's plan. Idempotent per day."""
    today = pacific_day(now)
    by_id = {c["history"].video.id: c["history"] for c in contexts}
    evaluated = evaluate_actions(session, now, by_id, base)
    session.flush()
    record = track_record(session)
    ranking = []
    for c in contexts:
        if budget:
            budget.check()
        video, history, f, regime = c["video"], c["history"], c["features"], c["regime"]
        momentum = live_momentum(session, video.id)
        peak = historical_peak(history, today)
        rev = revival(f, regime, base, peak)
        external = external_opportunity(session, video.id)
        board = scores(f, regime, base, c.get("forecasts", []), momentum, peak, external)
        state = state_of(f, regime, base, rev)
        conf = c["recommendation"]["confidence"] if c.get("recommendation") else v4_confidence(base, None, {})
        pending = session.scalar(select(GrowthAction).where(GrowthAction.video_id == video.id, GrowthAction.status == "pending")
                                 .order_by(GrowthAction.created_day.desc()))
        if pending and state not in ("protect_momentum", "paid_excluded", "paid_cooldown") and pending.action != "protect_no_change":
            action, notes = pending.action, [f"Aktion vom {pending.created_day} läuft noch bis zur Auswertung; keine tägliche Kurskorrektur."]
            details = {**pending.payload, "notes": notes, "held_since": str(pending.created_day)}
        else:
            action, notes = choose_action(state, f, rev, base, c.get("experiments", []), record, external)
            details = action_details(action, state, f, regime, base, board, rev, momentum, conf, notes, c.get("experiments", []), external)
            if pending and (pending.action != action):
                pending.status, pending.outcome = "superseded", "inconclusive"
                pending.evaluation = {"reason": f"Ersetzt durch {action} wegen Zustand {state}.", "superseded_on": str(today)}
                pending.evaluated_at = now
                pending = None
            if pending is None:
                statement = upsert(session, GrowthAction).values(video_id=video.id, created_day=today, created_at=now, version=VERSION,
                    state=state, action=action, target_metric=details["target_metric"], window_days=details["window_days"],
                    evaluate_after=today+timedelta(days=details["window_days"]+lag_days()), status="pending", payload=details)
                session.execute(statement.on_conflict_do_nothing(index_elements=["video_id", "created_day"]))
        paid, profile = paid_state(f)
        if f is None:
            profile = paid_profile(history, today)
            paid = profile["status"]
        statement = upsert(session, GrowthScore).values(video_id=video.id, day=today, version=VERSION, state=state, action=action,
            opportunity=board["opportunity"], viewer=board["viewer"], subscriber=board["subscriber"], revival=rev,
            momentum={**(momentum or {}), "paid": profile}, created_at=now)
        session.execute(statement.on_conflict_do_update(index_elements=["video_id", "day", "version"],
            set_={k: getattr(statement.excluded, k) for k in ("state", "action", "opportunity", "viewer", "subscriber", "revival", "momentum", "created_at")}))
        ranking.append({"video_id": video.id, "title": video.title, "state": state, "regime": regime["regime"], "breakout": regime["regime"] in PROTECT_REGIMES,
            "paid_status": paid, "paid_label": PAID_LABELS.get(paid, paid), "paid": profile,
            "action": action, "opportunity_score": board["opportunity"]["score"], "viewer_score": board["viewer"]["score"],
            "subscriber_score": board["subscriber"]["score"], "revival": rev["candidate"], "revival_signals": rev["signals"],
            "confidence": conf["level"], "reason": details["reason"], "notes": details.get("notes", []), "window_days": details["window_days"],
            "target_metric": details["target_metric"], "success_criterion": details["success_criterion"], "objective": details["objective"],
            "do_not_change": details["do_not_change"], "next_evaluation": str(today+timedelta(days=details["window_days"]+lag_days())),
            "held_since": details.get("held_since"), "momentum": momentum,
            "external": {"score": external.get("score"), "kind": external.get("kind"), "key": external.get("key"), "gap": external.get("gap"),
                         "audience": external.get("audience"), "demand_source": external.get("demand_source"),
                         "subscriber_fit": (external.get("scores") or {}).get("subscriber_fit_score")} if external else None})
    ranking.sort(key=lambda r: (r["opportunity_score"] is None, -(r["opportunity_score"] or 0), -(r["viewer_score"] or 0)))
    # Paid history and current paid state must remain auditable even where scores exist.
    for r in ranking:
        r["paid_note"] = (f"{r['paid'].get('paid_days_total', 0)} Werbetage in der Historie, zuletzt {r['paid'].get('last_paid_day')}" if r["paid"].get("paid_days_total") else "nie beworben")
    for i, r in enumerate(ranking):
        r["priority"] = i+1
    plan = daily_plan(ranking, today, record)
    statement = upsert(session, GrowthPlan).values(day=today, version=VERSION, created_at=now, plan=plan)
    session.execute(statement.on_conflict_do_update(index_elements=["day", "version"], set_={"plan": statement.excluded.plan, "created_at": statement.excluded.created_at}))
    session.commit()
    return {"evaluated_actions": evaluated, "ranked": len(ranking), "priority": plan.get("priority_video_id")}


def daily_plan(ranking, today, record):
    top = ranking[0] if ranking else None
    subscriber = max((r for r in ranking if r["subscriber_score"] is not None), key=lambda r: r["subscriber_score"], default=None)
    viewer = max((r for r in ranking if r["viewer_score"] is not None), key=lambda r: r["viewer_score"], default=None)
    if top is None:
        return {"day": str(today), "version": VERSION, "status": "insufficient_data", "ranking": [], "note": SCORE_NOTE}
    objective = top["objective"]
    if subscriber and subscriber["video_id"] == top["video_id"] and top["action"] in ("observe", "cross_promote") \
            and (top["subscriber_score"] or 0) > (top["viewer_score"] or 0):
        objective = "Subscriber"
    ext = top.get("external")
    internal = {"state": top["state"], "regime": top["regime"], "breakout": top["breakout"], "opportunity_score": top["opportunity_score"],
                "viewer_score": top["viewer_score"], "subscriber_score": top["subscriber_score"], "paid_status": top.get("paid_status"),
                "momentum_v2": (top.get("momentum") or {}).get("score")}
    external_block = {"available": bool(ext), "score": ext.get("score") if ext else None, "kind": ext.get("kind") if ext else None,
                      "key": ext.get("key") if ext else None, "gap": ext.get("gap") if ext else None, "audience": ext.get("audience") if ext else None,
                      "demand_source": ext.get("demand_source") if ext else None, "note": "Externe Nachfrage-Signale sind Proxies, außer sie stammen aus eigenen Analytics."}
    combined = (f"{top['title']}: interner Zustand {top['state']}" + (f" + externe Chance „{ext['key']}“ ({ext['demand_source']})" if ext else " ohne externe Chance")
                + f" → {top['action']}.")
    return {"day": str(today), "version": VERSION, "status": "ok" if top["opportunity_score"] is not None else "insufficient_data",
            "internal_signals": internal, "external_signals": external_block, "combined_decision": combined,
            "priority_video_id": top["video_id"], "priority_title": top["title"], "why": top["reason"],
            "why_priority": f"Höchster Growth-Opportunity-Score ({top['opportunity_score']}) im Ranking; Zustand {top['state']}." if top["opportunity_score"] is not None
                            else "Kein Video mit belastbarem Score; Daten sammeln.",
            "action": top["action"], "objective": objective, "do_not_change": top["do_not_change"], "success_metric": top["target_metric"],
            "success_criterion": top["success_criterion"], "window_days": top["window_days"], "next_evaluation": top["next_evaluation"],
            "confidence": top["confidence"], "subscriber_focus": subscriber["video_id"] if subscriber else None,
            "viewer_focus": viewer["video_id"] if viewer else None, "ranking": ranking, "track_record": record,
            "note": SCORE_NOTE+" "+GENERALIZATION_NOTE, "read_only": "Keine automatischen Änderungen auf YouTube."}


def overview(session):
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.day.desc(), GrowthPlan.id.desc()))
    actions = {}
    for row in session.scalars(select(GrowthAction).order_by(GrowthAction.created_day.desc())):
        actions.setdefault(row.video_id, []).append({"id": row.id, "created_day": row.created_day, "state": row.state, "action": row.action,
            "status": row.status, "outcome": row.outcome, "target_metric": row.target_metric, "window_days": row.window_days,
            "evaluate_after": row.evaluate_after, "evaluation": row.evaluation, "payload": row.payload})
    scores_by_video = {}
    for video in session.scalars(select(Video).where(Video.active.is_(True))):
        row = session.scalar(select(GrowthScore).where(GrowthScore.video_id == video.id).order_by(GrowthScore.day.desc()))
        if row:
            scores_by_video[video.id] = {"day": row.day, "state": row.state, "action": row.action, "opportunity": row.opportunity,
                                         "viewer": row.viewer, "subscriber": row.subscriber, "revival": row.revival, "momentum": row.momentum}
    return {"version": VERSION, "plan": plan.plan if plan else None, "plan_day": plan.day if plan else None,
            "scores": scores_by_video, "actions": {k: v[:10] for k, v in actions.items()}, "track_record": track_record(session),
            "states": STATES, "actions_catalog": ACTIONS, "read_only": True}
