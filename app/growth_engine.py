"""V5 Active Organic Growth Engine: rank, decide, protect, measure – never write to YouTube.

Scores are relative priorities on this channel (0–100), assembled from components
that are actually available; missing inputs are listed, never imputed. States and
actions follow V4 regimes and video-balanced channel baselines. Every action is a
recommendation with success and stop criteria and is later scored against observed
analytics. Confidence inherits the V4 caps: with few videos it never exceeds "low".
"""
from copy import deepcopy
from datetime import date, timedelta
import logging
from math import tanh
from sqlalchemy import select
from .models import (GrowthAssessment, GrowthScore, GrowthAction, GrowthPlan, AnalyticsForecast, ChannelPlaylist,
                     Decision, DiscoveryRun, Video, utcnow)
from .backfill import upsert
from .history import pacific_day, lag_days, features_at, paid_profile, PAID_WINDOW_DAYS
from .metrics import aware
from .strategy import confidence as v4_confidence, NO_MANIPULATION, GENERALIZATION_NOTE
from .regimes import usable_median, MIN_WEEKLY_VIEWS
from .discovery import best_for_video as external_opportunity

log = logging.getLogger(__name__)
VERSION = "growth-v5"
STATES = ["protect_momentum", "scale_opportunity", "needs_packaging_test", "needs_retention_analysis", "needs_discovery",
          "needs_distribution", "revival_candidate", "observe", "paid_cooldown", "paid_excluded", "insufficient_data"]
PAID_LABELS = {"organic": "Aktuell organisch", "organic_with_paid_history": "Aktuell organisch – historisch Werbung vorhanden",
               "paid_cooldown": "Paid-Cooldown", "paid_excluded": "Aktuell Paid beeinflusst"}
ACTIONS = ["protect_no_change", "test_title", "test_thumbnail", "test_title_thumbnail", "investigate_retention",
           "improve_discovery", "cross_promote", "create_followup_content", "observe",
           "target_search_opportunity", "target_suggested_cluster", "packaging_for_audience", "revive_existing_video",
           "distribute_playlist_context", "probe_missing_evidence",
           # Nach dem Feasibility-Check: jede dieser Aktionen setzt genau eine Ressource voraus, deren Existenz belegt ist.
           "link_from_own_video", "place_in_existing_playlist", "create_playlist_context"]
PLAYLIST_STALE_DAYS = 3   # Aelteres Inventar gilt als unbekannt – "unbekannt" ist nicht "keine vorhanden".
SOURCE_LEAD_FACTOR = 2.0  # Nur ein deutlich bestausgeliefertes Video wird ohne Rueckfrage benannt.
EXTERNAL_MIN_SCORE = 60  # Relative external score needed before an external opportunity may drive the action.
EXTERNAL_STATES = ("observe", "needs_discovery", "needs_distribution", "needs_packaging_test", "scale_opportunity", "revival_candidate")
ABS_CTR_OK = 0.04         # Without a usable channel median, 4 % click-through is not a packaging problem.
LOW_IMPRESSIONS_7D = 300  # Absolute delivery floor used only until the channel has its own impressions median.
SCARCE_SHARE = 0.5        # Below half the channel median of impressions, delivery is the bottleneck.
MIN_ROUTE_VIEWS = 10      # Below this the 7-day traffic mix is noise and names no route.
WEAK_SOURCE_VIEWS_7D = 20 # A source below this can barely pass on traffic – say so before anyone spends effort.
QUEUE_LIMIT = 3           # A short daily queue: never ten simultaneous changes on one channel.
ROUTE_LABELS = {"traffic_search": "YouTube-Suche", "traffic_suggested": "Empfehlungen neben anderen Videos",
                "traffic_browse": "Kanalseite, Playlists, Startseite, Endscreens", "traffic_external": "externe Links"}
GAP_ACTIONS = {"existing_video_opportunity": "target_search_opportunity", "search_opportunity": "target_search_opportunity",
               "suggested_opportunity": "target_suggested_cluster", "packaging_opportunity": "packaging_for_audience",
               "followup_content_opportunity": "create_followup_content"}
PROTECT_REGIMES = ("breakout", "breakout_candidate", "accelerating")
DISCOVERY_KEYS = ("traffic_search", "traffic_suggested", "traffic_browse")
SCORE_NOTE = "Relativer Priorisierungswert für diesen Kanal (0–100), keine Wahrscheinlichkeit."
OBJECTIVES = {"protect_no_change": "Discovery", "test_title": "Viewer", "test_thumbnail": "Viewer", "test_title_thumbnail": "Viewer",
              "investigate_retention": "Watchtime", "improve_discovery": "Discovery", "cross_promote": "Discovery",
              "create_followup_content": "Subscriber", "observe": "Watchtime", "target_search_opportunity": "Viewer",
              "target_suggested_cluster": "Discovery", "packaging_for_audience": "Viewer", "revive_existing_video": "Viewer",
              "distribute_playlist_context": "Discovery", "probe_missing_evidence": "Discovery",
              "link_from_own_video": "Discovery", "place_in_existing_playlist": "Discovery",
              "create_playlist_context": "Discovery"}
WINDOWS = {"protect_no_change": 7, "test_title": 14, "test_thumbnail": 14, "test_title_thumbnail": 14, "investigate_retention": 14,
           "improve_discovery": 14, "cross_promote": 14, "create_followup_content": 30, "observe": 7,
           "target_search_opportunity": 28, "target_suggested_cluster": 28, "packaging_for_audience": 14, "revive_existing_video": 28,
           "distribute_playlist_context": 14, "probe_missing_evidence": 14,
           "link_from_own_video": 14, "place_in_existing_playlist": 14, "create_playlist_context": 28}
TARGETS = {"protect_no_change": "views_7d", "test_title": "views_7d", "test_thumbnail": "ctr_or_views", "test_title_thumbnail": "ctr_or_views",
           "investigate_retention": "watch_minutes_7d", "improve_discovery": "discovery_views_7d", "cross_promote": "views_7d",
           "create_followup_content": "subscribers_7d", "observe": "views_7d", "target_search_opportunity": "discovery_views_7d",
           "target_suggested_cluster": "discovery_views_7d", "packaging_for_audience": "ctr_or_views", "revive_existing_video": "views_7d",
           "distribute_playlist_context": "discovery_views_7d", "probe_missing_evidence": "impressions_7d",
           "link_from_own_video": "discovery_views_7d", "place_in_existing_playlist": "discovery_views_7d",
           "create_playlist_context": "discovery_views_7d"}
DO_NOT_CHANGE = {"protect_no_change": ["Titel", "Thumbnail", "Beschreibung/Tags", "Sichtbarkeit", "Endscreens des Videos"],
                 "test_title": ["Thumbnail", "Beschreibung", "Kapitel"], "test_thumbnail": ["Titel", "Beschreibung", "Kapitel"],
                 "test_title_thumbnail": ["Beschreibung", "Videoinhalt", "Sichtbarkeit"],
                 "investigate_retention": ["Titel", "Thumbnail"], "improve_discovery": ["Titel", "Thumbnail", "Videoinhalt"],
                 "cross_promote": ["Titel und Thumbnail des beworbenen Videos"], "create_followup_content": ["Bestehendes Video"],
                 "observe": ["Alles – zuerst messen"], "target_search_opportunity": ["Thumbnail", "Videoinhalt"],
                 "target_suggested_cluster": ["Titel", "Thumbnail", "Videoinhalt"], "packaging_for_audience": ["Videoinhalt", "Sichtbarkeit"],
                 "revive_existing_video": ["Videoinhalt", "Sichtbarkeit"],
                 # Distribution-Experimente fassen das Paket ausdrücklich nicht an: sonst misst man zwei Dinge gleichzeitig.
                 "distribute_playlist_context": ["Titel", "Thumbnail", "Beschreibung", "Videoinhalt", "Sichtbarkeit"],
                 "probe_missing_evidence": ["Titel", "Thumbnail", "Beschreibung", "Videoinhalt", "Sichtbarkeit"],
                 "link_from_own_video": ["Titel", "Thumbnail", "Beschreibung", "Videoinhalt", "Sichtbarkeit",
                                         "Titel/Thumbnail des Quellvideos"],
                 "place_in_existing_playlist": ["Titel", "Thumbnail", "Beschreibung", "Videoinhalt", "Sichtbarkeit",
                                                "Name der Playlist"],
                 "create_playlist_context": ["Titel", "Thumbnail", "Beschreibung", "Videoinhalt", "Sichtbarkeit"]}
MIN_TRACK_RECORD = 3
# Genau ein veraenderlicher Hebel je Experiment. Wer Beschreibung, Playlist und Endscreen gleichzeitig
# anfasst, kann das Ergebnis keiner Ursache zuordnen; alles Weitere wird ein eigenes Experiment.
LEVERS = {"protect_no_change": "keiner – bewusst keine Änderung", "observe": "keiner – nur messen",
          "test_title": "Titel", "test_thumbnail": "Thumbnail",
          "test_title_thumbnail": "Titel und Thumbnail gemeinsam (nicht trennbar, bewusst akzeptiert)",
          "investigate_retention": "keiner – nur Analyse",
          "improve_discovery": "interne Verlinkung: bestehende Playlist/eigenes Video → dieses Video",
          "cross_promote": "interne Verlinkung aus dem stärkeren eigenen Video",
          "create_followup_content": "neues Video", "target_search_opportunity": "Wortlaut in Beschreibung und Kapiteln",
          "target_suggested_cluster": "Wortlaut in Beschreibung und Playlist-Benennung",
          "packaging_for_audience": "genau eine Packaging-Dimension",
          "revive_existing_video": "interne Verlinkung und Playlist-Platzierung",
          "distribute_playlist_context": "interne Verlinkung: bestehende Playlist/eigenes Video → dieses Video",
          "probe_missing_evidence": "interne Verlinkung aus einer belegten eigenen Ressource → dieses Video",
          "link_from_own_video": "Endscreen/Infokarte aus einem benannten eigenen Video → dieses Video",
          "place_in_existing_playlist": "Platzierung in einer benannten, real existierenden Playlist",
          "create_playlist_context": "neue thematische Playlist anlegen und dieses Video aufnehmen"}
# Was bewusst NICHT mitgeändert wird, sondern ein eigenes Experiment bleibt.
DEFERRED_LEVERS = {"distribute_playlist_context": ["Beschreibungstext auf ein belegtes Thema ausrichten",
                                                  "Titel oder Thumbnail testen"],
                   "probe_missing_evidence": ["Beschreibungstext auf ein belegtes Thema ausrichten",
                                              "Titel oder Thumbnail testen"],
                   "improve_discovery": ["Beschreibungstext auf ein belegtes Thema ausrichten"],
                   "link_from_own_video": ["Playlist anlegen oder bestücken", "Beschreibungstext ausrichten",
                                           "Titel oder Thumbnail testen"],
                   "place_in_existing_playlist": ["Endscreen-Verlinkung", "Beschreibungstext ausrichten",
                                                  "Titel oder Thumbnail testen"],
                   "create_playlist_context": ["Endscreen-Verlinkung", "Beschreibungstext ausrichten",
                                               "Titel oder Thumbnail testen"],
                   "target_search_opportunity": ["Playlist-Platzierung", "Titel"],
                   "target_suggested_cluster": ["Titel", "Thumbnail"]}
# Die beeinflussbaren Eigenschaften des eigenen Assets, die auf algorithmische Auslieferung zielen.
# Interne Wegeleitung (Endscreen, Playlist, Verlinkung) ist ausdruecklich KEIN Growth-Mechanismus und
# niemals ein Ersatz, nur damit ein Experiment existiert.
GROWTH_LEVERS = ("test_title", "test_thumbnail", "test_title_thumbnail", "packaging_for_audience",
                 "target_search_opportunity", "target_suggested_cluster", "improve_discovery",
                 "revive_existing_video", "investigate_retention")
ROUTING_LEVERS = ("link_from_own_video", "place_in_existing_playlist", "create_playlist_context",
                  "probe_missing_evidence", "cross_promote")
# Welche YouTube-Discovery-Flaeche eine Maßnahme beeinflussen soll – gehoert in jeden Vorschlag.
DISCOVERY_SURFACES = {
    "test_title": "Browse/Home und Suggested: Klickentscheidung bei bestehender Auslieferung",
    "test_thumbnail": "Browse/Home und Suggested: Klickentscheidung bei bestehender Auslieferung",
    "test_title_thumbnail": "Browse/Home und Suggested: Klickentscheidung bei bestehender Auslieferung",
    "packaging_for_audience": "Browse/Home: Auslieferung an die belegte Zielgruppe",
    "target_search_opportunity": "YouTube-Suche: Treffer auf real gemessene Suchbegriffe",
    "target_suggested_cluster": "Suggested/Related: Nachbarschaft der Videos, neben denen wir ausgeliefert werden",
    "improve_discovery": "Browse und Suggested: thematische Einordnung des Videos",
    "revive_existing_video": "Browse/Suggested: erneute Auslieferung eines vorhandenen Videos",
    "investigate_retention": "Suggested: Sitzungswert entscheidet, ob YouTube weiter ausliefert",
    "link_from_own_video": "Interne Wegeleitung (keine algorithmische Flaeche)",
    "place_in_existing_playlist": "Playlist-Seite und Autoplay (interne Wegeleitung)",
    "create_playlist_context": "Playlist-Seite (interne Wegeleitung)",
    "probe_missing_evidence": "Evidenzbeschaffung, keine algorithmische Flaeche",
    "cross_promote": "Interne Wegeleitung (keine algorithmische Flaeche)",
    "observe": "keine", "protect_no_change": "keine", "create_followup_content": "Browse/Suggested (neues Video)"}
# Belastbarkeit der spaeteren Aussage – getrennt von der Frage, ob die Hypothese zulaessig ist.
RELIABLE, INDICATIVE = "belastbar", "indikativ"
# Aktionen, deren Zweck Datenerzeugung oder Messbarkeit ist. Sie sind interne Mittel und gehoeren nicht in
# JETZT TUN: der Nutzer will zusaetzliche organische Reichweite, nicht mehr Evidenz.
EVIDENCE_ONLY = ("probe_missing_evidence", "create_playlist_context")
EVIDENCE_LEVEL_RANK = {"own_analytics": 3, "multi_signal_proxy": 2, "weak_proxy": 1}
STRONG_EVIDENCE = ("own_analytics", "multi_signal_proxy")
MIN_MEASURABLE_VIEWS_7D = 3        # Darunter kann ein Effekt nicht von Rauschen getrennt werden.
MIN_MEASURABLE_IMPRESSIONS_7D = 10
# Lebenszyklus einer Maßnahme. Ohne Bestätigung durch den Menschen bleibt sie ein Vorschlag:
# das System hat keine Schreibrechte auf YouTube und darf deshalb nichts als "läuft" ausgeben.
PROPOSED, RUNNING, EVALUATED, SUPERSEDED = "proposed", "running", "evaluated", "superseded"
ACTION_STATUSES = (PROPOSED, RUNNING, EVALUATED, SUPERSEDED)


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
    if value is None or not usable_median(ref):
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
    if regime.get("low_activity"):
        # Tempo, Beschleunigung, Regime und Prognose sind bei dieser Menge Rauschen: nicht bewerten.
        pace = accel = regime_signal = forecast = uncertainty = live = None
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
        return usable_median(ref) and f.get(key) is not None and f[key] > ref["median"]
    def below(key):
        ref = med.get(key, {})
        return usable_median(ref) and f.get(key) is not None and f[key] < ref["median"]
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


def packaging_ok(f, base):
    """Is the package (title/thumbnail) demonstrably not the bottleneck?

    A video with a click-through rate at or above the channel median and retention that is not below it
    is being clicked when it is shown. Forcing a thumbnail or title test there tests the wrong thing.
    """
    if not f:
        return False, "Keine Features: Paketqualität unbekannt."
    ctr, med = f.get("ctr_7d"), (base or {}).get("medians", {})
    if ctr is None:
        return False, "CTR unbekannt (keine Impressions-Daten im Fenster)."
    ref = med.get("ctr_7d", {})
    if usable_median(ref):
        ok_ctr, why = ctr >= ref["median"], f"CTR {ctr*100:.1f} % vs Kanalmedian {ref['median']*100:.1f} %"
    else:
        ok_ctr, why = ctr >= ABS_CTR_OK, f"CTR {ctr*100:.1f} % (kein belastbarer Kanalmedian, Schwelle {ABS_CTR_OK*100:.0f} %)"
    retention, rref = f.get("retention_avg"), med.get("retention_avg", {})
    if retention is not None and usable_median(rref) and retention < rref["median"]*0.9:
        return False, why+f"; Retention {retention:.2f} unter Kanalmedian {rref['median']:.2f}"
    return bool(ok_ctr), why+("; Retention nicht unter Kanalmedian" if retention is not None else "; Retention unbekannt")


def distribution_scarce(f, base):
    """Is the video barely delivered at all? Measured against the channel's own impressions median."""
    if not f:
        return False, None
    impressions, ref = f.get("impressions_7d"), (base or {}).get("medians", {}).get("impressions_7d", {})
    if impressions is None:
        # Ohne Impressions-Daten ersatzweise die ausgelieferte Menge: keine Views heißt keine Verteilung.
        views = f.get("views_7d")
        return (views is not None and views < MIN_WEEKLY_VIEWS,
                {"basis": "views_7d", "views_7d": views, "threshold": MIN_WEEKLY_VIEWS, "impressions_7d": None})
    if usable_median(ref):
        return impressions < ref["median"]*SCARCE_SHARE, {"basis": "impressions_vs_channel_median", "impressions_7d": impressions,
                                                          "channel_median": ref["median"], "share": SCARCE_SHARE}
    return impressions < LOW_IMPRESSIONS_7D, {"basis": "impressions_absolute_floor", "impressions_7d": impressions,
                                              "threshold": LOW_IMPRESSIONS_7D,
                                              "note": "Kanalmedian für Impressions noch nicht belastbar."}


# Eine Maßnahme, deren Zweck es ist, ueberhaupt Auslieferung zu erzeugen, kann keine vorhandene
# Auslieferung zur Voraussetzung haben. Fuer sie verschiebt sich die Schwelle vom Eintritt zum Ergebnis:
# als Wirkung zaehlt erst, wenn im Messfenster wirklich lesbare Auslieferung entsteht (siehe _outcome).
COLD_START_ACTIONS = ("probe_missing_evidence", "create_playlist_context")


def measurable(baseline, target_metric=None, action=None):
    """Ohne messbare Ausgangsbasis kann ein Optimierungsexperiment nur „unklar“ ergeben – dann ist es keine Aufgabe."""
    views = (baseline or {}).get("views_7d") or 0
    impressions = (baseline or {}).get("impressions_7d") or 0
    if views >= MIN_MEASURABLE_VIEWS_7D or impressions >= MIN_MEASURABLE_IMPRESSIONS_7D:
        return True, None
    if action in COLD_START_ACTIONS:
        return True, (f"Cold Start: {views} Views und {impressions} Impressions sind der Befund, nicht die "
                      f"Voraussetzung. Diese Maßnahme soll Auslieferung erzeugen; als Wirkung zaehlt erst, wenn "
                      f"im Messfenster mindestens {MIN_MEASURABLE_IMPRESSIONS_7D} Impressions entstehen.")
    return False, (f"Keine messbare Ausgangsbasis ({views} Views und {impressions} Impressions in der letzten bekannten "
                   f"Woche; nötig {MIN_MEASURABLE_VIEWS_7D} Views oder {MIN_MEASURABLE_IMPRESSIONS_7D} Impressions). "
                   "Ein Experiment könnte hier nur „unklar“ ergeben.")


def known_route(f):
    """The strongest real traffic route of the last known week – the concrete starting point for distribution."""
    total = (f or {}).get("traffic_total_7d") or 0
    if total < MIN_ROUTE_VIEWS:
        return None
    key = max(ROUTE_LABELS, key=lambda k: (f.get(k) or 0))
    share = f.get(key)
    if not share:
        return None
    return {"key": key, "label": ROUTE_LABELS[key], "share": round(share, 3), "views_7d": total}


def state_of(f, regime, base, rev):
    status = regime["regime"]
    if status == "paid_excluded":
        paid, _ = paid_state(f)
        return "paid_excluded" if paid == "paid_excluded" else "paid_cooldown"
    if f is None:
        return "insufficient_data"
    if status in PROTECT_REGIMES:
        return "protect_momentum"     # Schutz geht allem voraus; low_activity kann nie ein Breakout sein.
    if regime.get("low_activity"):
        # Belegte Distributionslücke statt Rauschen-Momentum: handlungsfähig, aber nie geschützt.
        return "needs_distribution"
    scarce, _ = distribution_scarce(f, base)
    if status == "insufficient_data":
        # Ohne belastbare Kanal-Baseline ist „zu wenig Daten“ kein Endzustand, wenn die Auslieferung
        # nachweislich fast null ist: dann ist die Verteilung das Problem und messbar angreifbar.
        return "needs_distribution" if scarce else "insufficient_data"
    if status == "growing":
        return "scale_opportunity"
    if rev.get("candidate"):
        return "revival_candidate"
    med = base.get("medians", {})
    def below(key):
        ref = med.get(key, {})
        return usable_median(ref) and f.get(key) is not None and f[key] < ref["median"]
    if status == "declining" and (below("retention_avg") or below("pct_7d")):
        return "needs_retention_analysis"
    # Verteilung vor Verpackung: wer kaum ausgeliefert wird, hat kein Thumbnail-Problem, sondern ein Distributionsproblem.
    if scarce and packaging_ok(f, base)[0]:
        return "needs_distribution"
    if status == "declining" and (below("ctr_7d") or f.get("ctr_7d") is None):
        return "needs_packaging_test"
    share = discovery_share(f)
    ref = [med.get(k, {}) for k in DISCOVERY_KEYS]
    if share is not None and all(usable_median(r) for r in ref) and share < sum(r["median"] for r in ref)*0.8:
        return "needs_discovery"
    if scarce:
        # Auslieferung praktisch null: Beobachten ist hier kein Endzustand.
        return "needs_distribution"
    return "observe"


# ----------------------------------------------------------------------------- actions
def track_record(session):
    """Observed outcomes per action type and per evidence level; descriptive, not a causal effect estimate."""
    record, by_level = {}, {}
    for row in session.scalars(select(GrowthAction).where(GrowthAction.status == "evaluated",
                                                          GrowthAction.version == VERSION)):
        entry = record.setdefault(row.action, {"positive": 0, "negative": 0, "neutral": 0, "inconclusive": 0, "n": 0})
        entry[row.outcome] = entry.get(row.outcome, 0)+1
        entry["n"] += 1
        level = ((row.payload or {}).get("audience") or {}).get("evidence_level") or "ohne externe Evidenz"
        bucket = by_level.setdefault(level, {"positive": 0, "negative": 0, "neutral": 0, "inconclusive": 0, "n": 0})
        bucket[row.outcome] = bucket.get(row.outcome, 0)+1
        bucket["n"] += 1
    for entry in list(record.values())+list(by_level.values()):
        entry["net_negative"] = entry["n"] >= MIN_TRACK_RECORD and entry["negative"] > entry["positive"]
        entry["proven"] = False
        entry["basis"] = (f"{entry['positive']} positiv / {entry['negative']} negativ bei {entry['n']} ausgewerteten Fällen"
                          if entry["n"] >= MIN_TRACK_RECORD else
                          f"Nur {entry['n']} ausgewertete Fälle (< {MIN_TRACK_RECORD}): nichts bewiesen, keine Gewichtsänderung.")
    record["by_evidence_level"] = by_level
    return record


def distribution_action(f, base, external, channel=None):
    """Almost no delivery: run a low-risk distribution experiment, or fetch the evidence that is missing.

    Never a packaging test here – with a working click-through rate the package is not the bottleneck,
    and changing it would only contaminate the measurement of the distribution change.
    """
    ok, why = packaging_ok(f, base)
    route = known_route(f)
    _, detail = distribution_scarce(f, base)
    why = why.rstrip(".")
    head = f"Auslieferung zu gering ({detail.get('basis')}): "+(f"Paket ist nicht der Engpass – {why}." if ok else why+".")
    rejected = []
    if external and external.get("actionable") and not external.get("context_usable"):
        # Wortgleichheit ist keine Themengleichheit: die Chance bleibt sichtbar, gibt aber keinen Text vor.
        rejected = [f"Externe Chance „{external.get('key')}“ bleibt Hypothese und lenkt keinen Wortlaut: "
                    f"{external.get('context_reason') or 'thematische Relevanz nicht ausreichend belegt'}"]
    lever, lever_note = feasible_lever(channel)
    if lever is None or lever in EVIDENCE_ONLY:
        # Eine Playlist anzulegen oder Evidenz zu beschaffen erzeugt keine algorithmische Auslieferung.
        return "observe", [head]+rejected+[lever_note,
                "Daraus entsteht keine Aufgabe: eine Oberfläche nur zum Messen anzulegen bringt keine "
                "zusätzliche organische Reichweite."]
    if route:
        return lever, [head]+rejected+[f"Belegte eigene Route: {route['label']} "
                f"({route['share']*100:.0f} % von {route['views_7d']} Views der letzten bekannten Woche) – dort ansetzen, "
                "ohne Titel oder Thumbnail anzufassen.", lever_note]
    # Ohne belegte eigene Route und ohne tragfaehige Chance gibt es keine datenbegruendete Hypothese. Dann
    # entsteht hier ausdruecklich keine Maßnahme: eine Playlist anzulegen oder irgendwo einen Endscreen zu
    # setzen, nur damit ein Experiment existiert, erzeugt keine algorithmische Auslieferung.
    missing = [m["what"] for m in ((external or {}).get("missing_evidence") or [])][:3]
    return "observe", [head]+rejected+[
        "Keine datenbegründete Growth-Hypothese für dieses Video: "
        + ("es fehlt " + "; ".join(missing)+"." if missing else
           f"der eigene Quellenmix der letzten Woche hat weniger als {MIN_ROUTE_VIEWS} Views und nennt keine Route, "
           "und es liegt keine belegte Audience-/Placement-Chance vor."),
        "Interne Wegeleitung oder eine neue Playlist wären hier nur ein Ersatzversuch ohne algorithmische "
        "Wirkung – deshalb bewusst keine Maßnahme, bis echte Signale vorliegen."]


def baseline_snapshot(f):
    """The comparison window as it stands right now – stored unchanged when an experiment starts."""
    return {"known_end": (f or {}).get("known_end"), "views_7d": (f or {}).get("views_7d"),
            "impressions_7d": (f or {}).get("impressions_7d"), "ctr_7d": (f or {}).get("ctr_7d"),
            "retention_avg": (f or {}).get("retention_avg"), "traffic_total_7d": (f or {}).get("traffic_total_7d"),
            "discovery_share": discovery_share(f) if f else None,
            "note": "Vorher-Fenster sind die letzten 7 bekannten Analytics-Tage; Werbetage machen die Auswertung ungültig."}


def start_action(session, action_id, now=None):
    """The channel owner confirms they executed the proposal: freeze the baseline, start the window.

    This is the only transition into `running`. The system never performs it on its own – it has no
    write access to YouTube – and a second experiment on the same video is refused until this one is
    evaluated, so two changes can never be measured as one.
    """
    from .history import load as load_histories
    now = now or utcnow()
    today = pacific_day(now)
    row = session.get(GrowthAction, action_id)
    if row is None:
        raise LookupError("Maßnahme nicht gefunden.")
    if row.status == RUNNING:
        return row      # Idempotent: ein zweiter Klick startet nichts neu.
    if row.status != PROPOSED:
        raise ValueError(f"Nur ein offener Vorschlag kann gestartet werden; diese Maßnahme ist {row.status}.")
    from .acquisition import blocking
    other, reason = blocking(session, row.video_id, row.lever_class or "internal_link", row.traffic_source,
                             row.target_metric, exclude_id=row.id)
    if other is not None:
        # Gesperrt wird nur, was sich nicht sauber von einem laufenden Experiment trennen laesst.
        raise ValueError(f"Nicht trennbar von einer laufenden Maßnahme: {reason}")
    hist = next((h for h in load_histories(session) if h.video.id == row.video_id), None)
    features_now = features_at(hist, today) if hist is not None else None
    row.status, row.started_at, row.started_day = RUNNING, now, today
    row.evaluate_after = today+timedelta(days=row.window_days+lag_days())
    row.baseline = {**baseline_snapshot(features_now), "frozen_day": str(today), "frozen_at": aware(now).isoformat(),
                    "confirmed_by": "channel_owner",
                    "note": "Vom Kanalinhaber als durchgeführt bestätigt; das System hat nichts auf YouTube geändert."}
    session.commit()
    return row


def recent_results(session, limit=6):
    """Finished experiments with their observed outcome – the visible end of the learning loop."""
    out = []
    for row in session.scalars(select(GrowthAction).where(GrowthAction.status == EVALUATED,
                                                          GrowthAction.version == VERSION)
                               .order_by(GrowthAction.evaluated_at.desc()).limit(limit)):
        detail = ((row.evaluation or {}).get("detail") or {}) if isinstance((row.evaluation or {}).get("detail"), dict) else {}
        audience = (row.payload or {}).get("audience") or {}
        out.append({"video_id": row.video_id, "action": row.action, "created_day": str(row.created_day),
                    "window_days": row.window_days, "status": row.status, "outcome": row.outcome,
                    "metric": detail.get("metric"), "before": detail.get("before"), "after": detail.get("after"),
                    "relative_change": detail.get("relative_change"),
                    "reason": detail.get("reason") or (row.evaluation or {}).get("reason"),
                    "evidence_level": audience.get("evidence_level"), "demand_source": audience.get("demand_source"),
                    "note": "Beobachtete Veränderung im Messfenster, keine nachgewiesene Ursache."})
    return out


def strong_hypothesis(external, action):
    """Traegt diese Maßnahme eine datenbelegte Hypothese auf zusaetzliche algorithmische Auslieferung?

    Entscheidend ist die Evidenz hinter der Chance und ein Hebel am eigenen Asset – nicht, wie viel
    Auslieferung es heute schon gibt. Wenig Distribution ist der Growth-Fall, kein Ausschlussgrund.
    """
    if action not in GROWTH_LEVERS:
        return False
    external = external or {}
    return bool(external.get("actionable")) and external.get("evidence_level") in STRONG_EVIDENCE     # ohne Cold-Start-Ausnahme


def choose_action(state, f, rev, base, experiments, record, external=None, channel=None):
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
    elif (external and external.get("actionable") and (external.get("score") or 0) >= EXTERNAL_MIN_SCORE
            and external.get("context_usable") and state in EXTERNAL_STATES and external.get("gap") in GAP_ACTIONS):
        action = "revive_existing_video" if state == "revival_candidate" and external["gap"] != "packaging_opportunity" else GAP_ACTIONS[external["gap"]]
        notes = [f"Externe Chance ({external['kind']}: {external['key']}, Score {external['score']}, Evidenz: "
                 f"{external.get('evidence_level')}) lenkt die Aktion."]
    elif state == "needs_distribution":
        action, notes = distribution_action(f, base, external, channel)
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
    # Ressourcencheck vor allem anderen: eine Aktion, deren Voraussetzung nicht belegt ist, wird ersetzt.
    if action in RESOURCE_ACTIONS and not requirement(action, channel)["verified"]:
        lever, lever_note = feasible_lever(channel)
        if lever is None:
            action, notes = "observe", notes+[f"Nicht ausführbar: {lever_note}"]
        else:
            notes = notes+[f"Ressourcencheck: {LEVERS.get(action)} ist nicht belegt – stattdessen {LEVERS.get(lever)}. {lever_note}"]
            action = lever
    entry = record.get(action, {})
    if entry.get("net_negative") and action != "protect_no_change":
        notes.append(f"Beobachtete Bilanz für {action}: {entry['negative']} negativ / {entry['positive']} positiv – herabgestuft auf Beobachten (kein Kausalnachweis).")
        action = "observe"
    return action, notes


RESOURCE_ACTIONS = ("link_from_own_video", "probe_missing_evidence", "improve_discovery", "cross_promote",
                    "revive_existing_video", "place_in_existing_playlist", "create_playlist_context")


def requirement(action, channel):
    """The one resource this action presupposes – and the evidence that it exists."""
    channel = channel or {}
    if action in ("place_in_existing_playlist",):
        items = (channel.get("playlists") or {}).get("items") or []
        return {"kind": "playlist", "verified": bool(items),
                "named": [p["title"] for p in items[:3]], "evidence": (channel.get("playlists") or {}).get("note")}
    if action == "create_playlist_context":
        return {"kind": "none", "verified": True,
                "evidence": (channel.get("playlists") or {}).get("note"),
                "named": [], "note": "Legt die Ressource selbst an, setzt also keine voraus."}
    if action in RESOURCE_ACTIONS:
        chosen, candidates = channel.get("source"), channel.get("source_candidates") or []
        # Die IDs gehoeren dazu: sonst laesst sich spaeter nicht feststellen, welche eigene Ressource dieses
        # laufende Experiment veraendert – und sie waere fuer ein neues Experiment wieder freigegeben.
        return {"kind": "source_video", "verified": bool(chosen or candidates),
                "named": [chosen["title"]] if chosen else [c["title"] for c in candidates],
                "video_ids": [chosen["video_id"]] if chosen else [c["video_id"] for c in candidates],
                "evidence": chosen["evidence"] if chosen else "; ".join(c["evidence"] for c in candidates) or None}
    return {"kind": "none", "verified": True, "named": [], "evidence": None}


def choices_for(action, channel):
    """Real candidates to pick from when the system cannot determine the resource reliably."""
    channel = channel or {}
    if action in RESOURCE_ACTIONS and action not in ("place_in_existing_playlist", "create_playlist_context"):
        candidates = channel.get("source_candidates") or []
        if not channel.get("source") and len(candidates) > 1:
            return {"what": "source_video", "required": True,
                    "question": "Von welchem deiner Videos soll auf dieses Video verlinkt werden?",
                    "options": candidates}
    if action == "place_in_existing_playlist":
        items = (channel.get("playlists") or {}).get("items") or []
        if len(items) > 1:
            return {"what": "playlist", "required": True, "question": "In welche deiner Playlists soll das Video?",
                    "options": items}
    return None


def link_steps(title, channel, hold, f=None):
    """Link from a named own video – or let a human pick from the real candidates instead of guessing."""
    channel = channel or {}
    chosen, candidates = channel.get("source"), channel.get("source_candidates") or []
    if chosen:
        first = (f"Im YouTube Studio bei „{chosen['title']}“ einen Endscreen-Eintrag oder eine Info-Karte auf „{title}“ setzen "
                 f"(belegt ausgeliefert: {chosen['evidence']}).")
    elif candidates:
        options = "; ".join(f"„{c['title']}“ ({c['evidence']})" for c in candidates)
        first = (f"Eines dieser real existierenden eigenen Videos auswählen und dort einen Endscreen-Eintrag oder eine "
                 f"Info-Karte auf „{title}“ setzen: {options}. Das System wählt hier bewusst nicht für dich.")
    else:
        return None      # Ohne belegte Quelle gibt es keinen ausfuehrbaren Schritt - die Aktion wird gar nicht erst gewaehlt.
    steps = [first,
             "Am Quellvideo sonst nichts ändern – Titel, Thumbnail und Beschreibung dort bleiben unverändert.",
             f"An „{title}“ selbst nichts ändern: Titel, Thumbnail und Beschreibung bleiben unverändert."]
    reach = max((c["views_7d"] or 0) for c in candidates)
    if reach < WEAK_SOURCE_VIEWS_7D:
        # Ehrlich vor dem Aufwand: aus einer schwach ausgelieferten Quelle kann kaum Verkehr kommen.
        steps.append(f"Erwartungsmanagement: die beste verfügbare Quelle liefert selbst nur {reach} Views in der letzten "
                     "bekannten Woche. Der mögliche Effekt ist entsprechend klein und schwer von Rauschen zu trennen; "
                     "das Ergebnis zeigt vor allem, ob interne Verlinkung überhaupt Auslieferung erzeugt.")
    steps.append(hold)
    return steps


def playlist_steps(title, channel, hold, fit=""):
    """Only ever names a playlist that the API inventory actually reported."""
    items = ((channel or {}).get("playlists") or {}).get("items") or []
    if not items:
        return None      # Keine belegte Playlist: dann darf auch kein Schritt eine verlangen.
    names = "; ".join(f"„{p['title']}“ ({p.get('item_count')} Videos)" for p in items[:3])
    return [f"„{title}“ in eine dieser real existierenden Playlists aufnehmen{fit}: {names}. Position 1–3.",
            "Playlist nicht umbenennen und keine neue anlegen – das wäre ein eigenes Experiment.",
            f"An „{title}“ selbst nichts ändern: Titel, Thumbnail und Beschreibung bleiben unverändert.",
            hold]


def experiment_steps(action, title, f, external, channel):
    """Executable steps for a human: which video, which audience, which surface, in which order.

    Nothing here is performed by the system – YouTube stays read-only. Every step names a concrete
    object (playlist, end screen, description, the own video to link from) instead of a category.
    """
    route = known_route(f)
    # Ein fremdes Thema darf den Wortlaut nur vorgeben, wenn es belegt ist (context_usable);
    # sonst bleibt der Text neutral und die Chance ist nur eine zu pruefende Hypothese.
    proven = bool(external and external.get("context_usable"))
    audience = ((external or {}).get("audience") or (external or {}).get("key")) if proven else None
    # Thema, nicht Oberflaeche: eine Traffic-Route ist kein Playlist-Thema.
    topic = f"zu „{audience}“" if audience else "zum Thema dieses Videos"
    context = audience or "dieses Thema"
    surface = f" Ansatzpunkt laut eigenen Daten: {route['label']}." if route else ""
    # Eine belegte Audience hilft bei der Auswahl der bestehenden Playlist; geschrieben wird dadurch nichts.
    fit = f" (passend zu „{audience}“)" if audience else ""
    hold = "Während des Messfensters keine weitere Änderung an diesem Video – sonst misst du zwei Dinge gleichzeitig."
    link = link_steps(title, channel, hold, f)      # None, wenn keine belegte Quelle existiert
    steps = {
        # Ein Hebel, und nur belegte Ressourcen: was nicht nachweislich existiert, kommt in keinem Schritt vor.
        "link_from_own_video": link,
        "place_in_existing_playlist": playlist_steps(title, channel, hold, fit),
        "create_playlist_context": [
            f"Neue thematische Playlist anlegen (der Kanal hat laut Inventar keine) und „{title}“ als ersten Eintrag aufnehmen.",
            "Playlist öffentlich schalten und einen sprechenden Namen wählen; sie ist ab jetzt die Oberfläche, "
            "auf der weitere Videos eingeordnet werden können.",
            f"An „{title}“ selbst nichts ändern: Titel, Thumbnail und Beschreibung bleiben unverändert.",
            hold],
        "probe_missing_evidence": (link+[
            "Nach dem Messfenster entscheidet die Messung: Bleiben die Impressions praktisch null, ist die Auslieferung der "
            "Engpass und ein Packaging-Test wäre Verschwendung gewesen. Steigen sie, liegt eine belastbare Basis vor."]) if link else None,
        "target_search_opportunity": [
            f"Nur den Wortlaut: Suchintention „{(external or {}).get('key')}“ in die ersten zwei Beschreibungszeilen und in die "
            "Kapitelnamen aufnehmen, ohne Clickbait.",
            "Titel, Thumbnail und Playlist-Platzierung bleiben in diesem Fenster unverändert – das sind eigene Experimente.",
            hold],
        "target_suggested_cluster": [
            f"Nur den Wortlaut: Themenkontext {topic} in Beschreibung und Playlist-Benennung spiegeln."+surface,
            "Titel, Thumbnail und Endscreens bleiben in diesem Fenster unverändert.",
            hold],
        "improve_discovery": link,
        "cross_promote": link,
        "packaging_for_audience": [
            f"Genau eine Dimension ändern (Thumbnail ODER Titel) und dabei die Sprache der Zielgruppe „{context}“ verwenden.",
            "Alte Fassung sichern, Zeitpunkt der Änderung notieren; Beschreibung und Playlist bleiben unverändert.",
            hold],
        "revive_existing_video": link,
        "test_thumbnail": [f"Nur das Thumbnail von „{title}“ tauschen, alte Fassung sichern, Zeitpunkt notieren.", hold],
        "test_title": [f"Nur den Titel von „{title}“ ändern, alte Fassung sichern, Zeitpunkt notieren.", hold],
        "test_title_thumbnail": [f"Titel und Thumbnail von „{title}“ gemeinsam neu positionieren; beides dokumentieren.",
                                 "Wirkung ist danach nicht mehr auf eine Dimension zurückführbar – bewusst akzeptiert.", hold],
        "investigate_retention": [f"Retention-Kurve von „{title}“ auf Abbruchstellen prüfen (erste 30 Sekunden, Kapitelgrenzen).",
                                  "Erst nach dieser Analyse über Änderungen entscheiden – in diesem Fenster nichts ändern."],
        "create_followup_content": [f"Folgevideo zum Muster von „{title}“ planen und vor Veröffentlichung im Experiment Memory registrieren.",
                                    "Bestehendes Video unverändert lassen."],
    }
    return steps.get(action)


def action_details(action, state, f, regime, base, scoreboard, rev, momentum, conf, notes, experiments, external=None, channel=None, title=None):
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
        "distribute_playlist_context": "Kaum Auslieferung bei funktionierendem Paket: Verteilung über Playlist-Kontext und Endscreens erhöhen, "
                                       "ohne Titel oder Thumbnail zu verändern – sonst wird das Distributionsergebnis unlesbar.",
        "probe_missing_evidence": "Kaum Auslieferung und keine belastbare Hypothese: risikoarmes Distributionsexperiment, das genau die "
                                  "fehlende Evidenz erzeugt (wird das Video überhaupt ausgeliefert, wenn eine Quelle darauf verweist?).",
        "link_from_own_video": "Kaum Auslieferung bei funktionierendem Paket: Verteilung über eine belegte eigene Quelle erhöhen "
                               "(Endscreen/Infokarte), ohne Titel, Thumbnail oder Beschreibung zu verändern.",
        "place_in_existing_playlist": "Kaum Auslieferung bei funktionierendem Paket: dieses Video in eine real existierende eigene "
                                      "Playlist einsortieren, ohne das Paket zu verändern.",
        "create_playlist_context": "Es existiert keine Playlist und kein ausgeliefertes Quellvideo: die fehlende Oberfläche selbst "
                                   "anzulegen ist das Experiment – als eigener Schritt, nicht als stille Voraussetzung.",
    }[action]
    window = WINDOWS[action]
    target = TARGETS[action]
    success = {
        "protect_no_change": "7-Tage-Views bleiben ≥ 85 % des Vorher-Fensters; Regime bleibt wachsend/beschleunigend.",
        "observe": "Datenlage vollständig (Retention, Traffic, ggf. CTR) und Regime unverändert oder besser.",
        "probe_missing_evidence": "Impressions im Nachher-Fenster ≥ +15 % gegenüber Vorher und messbar über null; damit ist belegt, "
                                  "dass Auslieferung über Playlist/Endscreen erzeugbar ist. Bleiben sie bei ~0, ist das ebenfalls ein "
                                  "verwertbares Ergebnis: der Engpass liegt in der Verteilung, nicht im Paket.",
    }.get(action, f"{target} im Nachher-Fenster ≥ +15 % gegenüber Vorher-Fenster und über der typischen Wochenschwankung des Kanals; ohne Werbetraffic.")
    stop = ("Nicht anwendbar – keine Änderung." if action in ("protect_no_change", "observe") else
            "Playlist-Einordnung oder Endscreen zurücknehmen, wenn Views oder Watchtime ≥ 15 % unter dem Vorher-Fenster liegen; "
            "als negativ protokollieren. Titel und Thumbnail bleiben ohnehin unverändert."
            if action in ("distribute_playlist_context", "probe_missing_evidence") else
            "Views oder Watchtime im Nachher-Fenster ≥ 15 % unter Vorher, oder Retention fällt unter Kanalmedian: Änderung zurücknehmen und als negativ protokollieren.")
    missing = [m["what"] for m in ((external or {}).get("missing_evidence") or [])]
    if action == "probe_missing_evidence" and not missing:
        missing = [f"Eigener Quellenmix mit mindestens {MIN_ROUTE_VIEWS} Views in der letzten bekannten Woche",
                   "Eigene Suchbegriff-Details aus Analytics (YouTube liefert sie unterhalb der Aggregationsschwelle nicht)",
                   "Eine öffentliche Such-/Nachbarschaftsprobe zu einem spezifischen Begriff"]
    template = None
    if action in ("test_title", "test_thumbnail", "test_title_thumbnail", "create_followup_content", "improve_discovery", "cross_promote",
                  "target_search_opportunity", "target_suggested_cluster", "packaging_for_audience", "revive_existing_video",
                  "distribute_playlist_context", "probe_missing_evidence"):
        template = {"hypothesis": f"{action}: {reason}", "horizon_hours": 168 if window <= 14 else 720,
                    "design": "observational", "note": "Vor der Änderung im Experiment Memory registrieren; Zeitpunkt protokollieren."}
    return {"action": action, "state": state, "reason": reason, "notes": notes, "signals": signals, "against": against,
            "confidence": conf, "target_metric": target, "window_days": window, "success_criterion": success, "stop_criterion": stop,
            "discovery_surface": DISCOVERY_SURFACES.get(action, "algorithmische YouTube-Flächen"),
            "reliability": RELIABLE if measurable(baseline_snapshot(f), target, action)[0] else INDICATIVE,
            "steps": experiment_steps(action, title or "dieses Video", f, external, channel),
            "primary_lever": LEVERS.get(action), "deferred_levers": DEFERRED_LEVERS.get(action, []),
            "requires": requirement(action, channel), "choices": choices_for(action, channel),
            "needs_human_choice": bool(choices_for(action, channel)),
            "one_lever_note": "Genau ein veränderlicher Hebel. Weitere Änderungen wären eigene Experimente – "
                              "sonst ist das Ergebnis keiner Ursache zuzuordnen.",
            "missing_evidence": missing, "route": known_route(f),
            "baseline": baseline_snapshot(f),
            "executed_automatically": False,
            "do_not_change": DO_NOT_CHANGE[action]+[NO_MANIPULATION], "objective": OBJECTIVES[action],
            "experiment_template": template, "linked_decision_ids": [e["decision_id"] for e in experiments],
            "audience": {"target": external.get("audience"), "opportunity": external.get("key"), "kind": external.get("kind"), "gap": external.get("gap"),
                         "score": external.get("score"), "demand_source": external.get("demand_source"), "shared_tokens": external.get("shared_tokens"),
                         "evidence_level": external.get("evidence_level"), "families": external.get("families") or [],
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
    return {"days": len(days), "observed_days": len(rows), "views": views, "velocity": views/len(days),
            "sources": {s: v for s, v in sorted(totals.items(), key=lambda kv: -kv[1])},
            "watch_minutes": sum(r.watch_minutes for r in rows),
            "impressions": impressions if reach else None,
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
    for row in session.scalars(select(GrowthAction).where(GrowthAction.status == RUNNING,
                                                          GrowthAction.version == VERSION)):
        # Ein nie gestarteter Vorschlag wird nie als Erfolg oder Misserfolg gewertet.
        start = row.started_day or row.created_day
        after_end = start+timedelta(days=row.window_days)
        if after_end > known_end:
            continue
        history = by_id.get(row.video_id)
        if history is None:
            continue
        before = _window_metrics(history, start-timedelta(days=row.window_days), start-timedelta(days=1))
        after = _window_metrics(history, start+timedelta(days=1), after_end)
        regime_after = None
        f_after = features_at(history, after_end+timedelta(days=lag_days()))
        if f_after is not None:
            from .regimes import classify
            regime_after = classify(f_after, base)["regime"]
        outcome, detail = _outcome(row, before, after, base)
        linked = list(session.scalars(select(Decision.id).where(Decision.video_id == row.video_id, Decision.status.in_(["registered", "evaluated"]),
            Decision.created_at >= aware(row.created_at)-timedelta(days=1))))
        payload = row.payload or {}
        audience = payload.get("audience") or {}
        row.status, row.outcome, row.evaluated_at = EVALUATED, outcome, now
        row.evaluation = {"before": before, "after": after, "regime_after": regime_after, "detail": detail,
                          "hypothesis": {"lever": row.action, "surface": payload.get("discovery_surface"),
                                         "audience": audience.get("target"), "gap": audience.get("gap"),
                                         "evidence_level": audience.get("evidence_level"),
                                         "reliability": payload.get("reliability")},
                          "started_day": str(start), "frozen_baseline": row.baseline or {},
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
    elif target == "impressions_7d":
        # Auslieferung selbst ist die Zielgröße: ohne Impressions gibt es nichts zu verpacken.
        key, b, a = "impressions", before["impressions"], after["impressions"]
    elif target == "subscribers_7d":
        key, b, a = "subscribers", before["subscribers"], after["subscribers"]
    else:
        key, b, a = "views", before["views"], after["views"]
    if b is None or a is None:
        return "inconclusive", {"reason": f"Zielmetrik {key} nicht verfügbar."}
    change = (a-b)/max(1.0, b)
    noise = abs(base.get("accel_7d", {}).get("q75", 0.15)) if base else 0.15
    detail = {"metric": key, "before": b, "after": a, "relative_change": change, "channel_weekly_noise_q75": noise}
    if row.action in COLD_START_ACTIONS and (before.get("impressions") or 0) < MIN_MEASURABLE_IMPRESSIONS_7D:
        # Aus 5 auf 8 Impressions sind +60 %, aber weiterhin keine Auslieferung. Erst ab der Schwelle ist
        # das Ergebnis lesbar; darunter bleibt es ausdruecklich unklar.
        reached = (after.get("impressions") or 0) >= MIN_MEASURABLE_IMPRESSIONS_7D
        detail = {**detail, "cold_start_floor": MIN_MEASURABLE_IMPRESSIONS_7D, "reached_floor": reached}
        if not reached:
            return "inconclusive", {**detail,
                                    "reason": (f"Weiterhin unter {MIN_MEASURABLE_IMPRESSIONS_7D} Impressions im "
                                               "Messfenster: es ist keine Auslieferung entstanden, die man lesen "
                                               "kann.")}
        return ("positive" if change >= max(0.15, noise) else "neutral" if change > -0.15 else "negative"), detail
    if row.action == "protect_no_change":
        return ("positive" if change >= -0.15 else "negative"), detail
    if change >= max(0.15, noise):
        return "positive", detail
    if change <= -0.15:
        return "negative", detail
    return "neutral", detail


# ----------------------------------------------------------------------------- orchestration
# ----------------------------------------------------------------------------- Feasibility
def playlist_inventory(session, today):
    """What do we actually know about our own playlists? Unknown is not the same as none."""
    rows = list(session.scalars(select(ChannelPlaylist)))
    checked = None
    for run in session.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(10)):
        day = (run.stats or {}).get("playlists_checked_day")
        if day:
            checked = day
            break
    fresh = False
    if checked:
        try:
            fresh = (today-date.fromisoformat(str(checked))).days <= PLAYLIST_STALE_DAYS
        except ValueError:
            fresh = False
    items = [{"id": r.id, "title": r.title, "item_count": r.item_count, "privacy": r.privacy,
              "last_seen_day": str(r.last_seen_day)} for r in rows]
    if items:
        return {"state": "available", "items": items, "checked_day": checked,
                "note": f"{len(items)} eigene Playlist(s) laut API-Inventar vom {checked}."}
    if fresh:
        return {"state": "none", "items": [], "checked_day": checked,
                "note": f"Inventar vom {checked}: der Kanal hat keine Playlist. Eine bestehende Playlist kann "
                        "deshalb nicht Voraussetzung eines Experiments sein."}
    return {"state": "unknown", "items": [], "checked_day": checked,
            "note": "Playlists wurden noch nicht abgefragt – Existenz unbekannt, also keine Voraussetzung."}


def locked_resources(session):
    """Eigene Ressourcen, die ein bestaetigt laufendes Experiment gerade braucht – Ziel und Quelle.

    Bei Maßnahme #8 wird Trainstories gemessen, veraendert wurde aber der Endscreen von Shine On. Beide
    duerfen fuer ein neues Experiment nicht angefasst werden: am Zielvideo waere die Messung verfaelscht,
    an der Quelle ebenso. Bisher schuetzte die Sperre nur das gemessene Video.
    """
    locked = {}
    titles = {}
    for row in session.scalars(select(GrowthAction).where(GrowthAction.status == RUNNING)):
        info = {"action_id": row.id, "video_id": row.video_id, "until": str(row.evaluate_after),
                "role": "gemessenes Video"}
        locked[row.video_id] = info
        requires = ((row.payload or {}).get("requires") or {})
        if requires.get("kind") != "source_video":
            continue
        ids = list(requires.get("video_ids") or [])
        if not ids and requires.get("named"):
            # Aeltere Maßnahmen haben nur den Titel festgehalten: dann ueber den Titel aufloesen.
            if not titles:
                titles = {v.title: v.id for v in session.scalars(select(Video))}
            ids = [titles[name] for name in requires["named"] if name in titles]
        for video_id in ids:
            locked.setdefault(video_id, {**info, "video_id": video_id, "role": "veraenderte Quellressource"})
    return locked


def source_candidates(contexts, exclude_id, locked=None, limit=3):
    """Own videos that are actually delivered – only those can pass traffic on via end screen or card."""
    locked = locked or {}
    out = []
    for c in contexts:
        video, f = c["history"].video, c.get("features") or {}
        if video.id == exclude_id or video.id in locked:
            continue
        impressions, views = f.get("impressions_7d") or 0, f.get("views_7d") or 0
        if impressions <= 0 and views <= 0:
            continue    # Ein Video ohne eigene Auslieferung kann keine weitergeben.
        out.append({"video_id": video.id, "title": video.title, "impressions_7d": impressions, "views_7d": views,
                    "evidence": f"{views} Views und {impressions} Impressions in der letzten bekannten Woche"})
    return sorted(out, key=lambda x: (-x["impressions_7d"], -x["views_7d"]))[:limit]


def unique_source(candidates):
    """One clearly best delivered video, or None – then the choice belongs to a human, not to a guess."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    lead = max(candidates[0]["impressions_7d"], candidates[0]["views_7d"])
    second = max(candidates[1]["impressions_7d"], candidates[1]["views_7d"])
    return candidates[0] if lead >= SOURCE_LEAD_FACTOR*max(1, second) else None


def feasible_lever(channel):
    """Which distribution lever can actually be executed with resources whose existence is verified?"""
    channel = channel or {}
    candidates = channel.get("source_candidates") or []
    playlists = channel.get("playlists") or {"state": "unknown", "items": []}
    if candidates:
        chosen = channel.get("source")
        if chosen:
            return "link_from_own_video", (f"Belegtes Quellvideo: „{chosen['title']}“ ({chosen['evidence']}).")
        return "link_from_own_video", ("Mehrere real existierende Quellvideos kommen infrage – die Auswahl trifft ein "
                                       "Mensch, das System rät nicht.")
    if playlists.get("state") == "available":
        return "place_in_existing_playlist", (f"Real existierende Playlist: „{playlists['items'][0]['title']}“.")
    if playlists.get("state") == "none":
        # Geprüft und nachweislich keine Playlist: das Anlegen ist dann selbst das Experiment.
        return "create_playlist_context", ("Keine belegte Ressource vorhanden: weder ein ausgeliefertes eigenes Quellvideo "
                                           f"noch eine Playlist. {playlists.get('note', '')}").strip()
    held = channel.get("locked_sources") or []
    if held:
        # Transparent blockieren statt eine geschuetzte Ressource anzufassen.
        names = "; ".join(f"„{h['title']}“ ({h['role']} von Maßnahme #{h['action_id']}, bis {h['until']})"
                          for h in held[:3])
        return None, (f"Keine freie eigene Quellressource: {names}. Solange ein Experiment laeuft, wird daran "
                      "nichts verändert – weder am gemessenen Video noch an der Quelle, sonst wäre die laufende "
                      "Messung verfälscht.")
    # Ungeprüft ist nicht „nicht vorhanden“: dann wird nichts behauptet und nichts vorgeschlagen.
    return None, ("Ressourcenlage ungeprüft: kein eigenes Video mit messbarer Auslieferung, und das Playlist-Inventar "
                  "wurde noch nicht abgefragt.")


def delivery_leader(contexts, exclude_id, locked=None):
    """The own video that is actually delivered best – the only credible place to link a starved video from."""
    locked = locked or {}
    best = None
    for c in contexts:
        video, f = c["history"].video, c.get("features") or {}
        if video.id == exclude_id or video.id in locked:
            continue
        key = ((f.get("impressions_7d") or 0), (f.get("views_7d") or 0))
        if key > (0, 0) and (best is None or key > best[0]):
            best = (key, video)
    return {"video_id": best[1].id, "title": best[1].title, "impressions_7d": best[0][0], "views_7d": best[0][1]} if best else None


def run(session, now, contexts, base, budget=None):
    """One growth pass: evaluate old actions, score, decide and write today's plan. Idempotent per day."""
    today = pacific_day(now)
    by_id = {c["history"].video.id: c["history"] for c in contexts}
    playlists = playlist_inventory(session, today)
    evaluated = evaluate_actions(session, now, by_id, base)
    session.flush()
    record = track_record(session)
    locked = locked_resources(session)
    by_title = {c["history"].video.id: c["history"].video.title for c in contexts}
    ranking = []
    for c in contexts:
        if budget:
            budget.check()
        video, history, f, regime = c["video"], c["history"], c["features"], c["regime"]
        momentum = live_momentum(session, video.id)
        peak = historical_peak(history, today)
        rev = revival(f, regime, base, peak)
        external = external_opportunity(session, video.id)
        if external is None or (not external.get("context_usable")
                                and external.get("evidence_level") != "own_analytics"):
            from .audience import placement_opportunity
            attested = placement_opportunity(session, video.id)
            # Der belegte Intent ersetzt die aeltere Route nur, wenn er mehr traegt als sie – und niemals
            # eine Chance aus eigenen Analytics: gemessene eigene Daten schlagen jeden Proxy.
            if attested is not None and (external is None
                                         or (attested.get("score") or 0) > (external.get("score") or 0)):
                external = attested
        board = scores(f, regime, base, c.get("forecasts", []), momentum, peak, external)
        state = state_of(f, regime, base, rev)
        conf = c["recommendation"]["confidence"] if c.get("recommendation") else v4_confidence(base, None, {})
        # Nur ein vom Menschen bestätigt gestartetes Experiment hält ein Video. Ein Vorschlag ist nur ein
        # Vorschlag: das System kann auf YouTube nichts ausführen, also läuft ohne Bestätigung auch nichts.
        candidates = source_candidates(contexts, video.id, locked)
        held = [{"title": by_title.get(vid) or vid, **info} for vid, info in locked.items() if vid != video.id]
        channel = {"delivery_leader": delivery_leader(contexts, video.id, locked), "playlists": playlists,
                   "source_candidates": candidates, "source": unique_source(candidates),
                   "locked_sources": held}
        running = session.scalar(select(GrowthAction).where(GrowthAction.video_id == video.id, GrowthAction.status == RUNNING,
                                                            GrowthAction.version == VERSION)
                                 .order_by(GrowthAction.started_day.desc()))
        proposed = session.scalar(select(GrowthAction).where(GrowthAction.video_id == video.id, GrowthAction.status == PROPOSED,
                                                             GrowthAction.version == VERSION)
                                  .order_by(GrowthAction.created_day.desc()))
        if running is not None:
            action, notes = running.action, [f"Bestätigt gestartet am {running.started_day}; läuft bis zur Auswertung am "
                                             f"{running.evaluate_after}. Bis dahin nichts weiter an diesem Video ändern."]
            details = {**running.payload, "notes": notes, "held_since": str(running.started_day)}
            current = running
        else:
            action, notes = choose_action(state, f, rev, base, c.get("experiments", []), record, external, channel)
            details = action_details(action, state, f, regime, base, board, rev, momentum, conf, notes, c.get("experiments", []), external,
                                     channel=channel, title=video.title)
            if proposed and proposed.action != action:
                # Ein nicht gestarteter Vorschlag wird täglich neu entschieden, statt ein Video zu blockieren.
                proposed.status, proposed.outcome = "superseded", "inconclusive"
                proposed.evaluation = {"reason": f"Nicht gestartet; ersetzt durch {action} wegen Zustand {state}.",
                                       "superseded_on": str(today), "note": "Vorschlag, nie ausgeführt – kein Ergebnis."}
                proposed.evaluated_at = now
                proposed = None
            if proposed is None:
                # Je Video und Tag existiert genau eine Zeile. Aendert sich die Entscheidung innerhalb des Tages,
                # wird sie aktualisiert statt verworfen - sonst zeigte die Queue auf eine bereits ersetzte Maßnahme.
                proposed = session.scalar(select(GrowthAction).where(GrowthAction.video_id == video.id,
                                                                     GrowthAction.created_day == today,
                                                                     GrowthAction.version == VERSION))
                if proposed is not None and proposed.status in (PROPOSED, SUPERSEDED):
                    proposed.status, proposed.state, proposed.action = PROPOSED, state, action
                    proposed.outcome, proposed.evaluation, proposed.evaluated_at = None, None, None
                    proposed.version, proposed.created_at = VERSION, now
                elif proposed is None:
                    proposed = GrowthAction(video_id=video.id, created_day=today, created_at=now, version=VERSION,
                                            state=state, action=action, status=PROPOSED, lever_class="internal_link")
                    session.add(proposed)
            if proposed is not None and proposed.status == PROPOSED:
                # Ein offener Vorschlag traegt immer den aktuellen Stand: sonst zeigte die Queue neue Schritte,
                # waehrend die gespeicherte Maßnahme noch die alten festhielte und beim Start einfriere.
                proposed.state, proposed.version = state, VERSION
                proposed.target_metric, proposed.window_days = details["target_metric"], details["window_days"]
                proposed.evaluate_after = today+timedelta(days=details["window_days"]+lag_days())
                proposed.payload, proposed.baseline = details, details.get("baseline") or {}
                session.flush()
            current = proposed
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
            "stop_criterion": details["stop_criterion"], "steps": details.get("steps"), "baseline": details.get("baseline"),
            "missing_evidence": details.get("missing_evidence") or [], "route": details.get("route"),
            "primary_lever": details.get("primary_lever"), "deferred_levers": details.get("deferred_levers") or [],
            "requires": details.get("requires"), "choices": details.get("choices"),
            "needs_human_choice": bool(details.get("needs_human_choice")),
            "action_id": current.id if current else None, "action_status": current.status if current else PROPOSED,
            "started_day": str(current.started_day) if current and current.started_day else None,
            "do_not_change": details["do_not_change"], "next_evaluation": str(today+timedelta(days=details["window_days"]+lag_days())),
            "held_since": details.get("held_since"), "momentum": momentum,
            "external": {"score": external.get("score"), "kind": external.get("kind"), "key": external.get("key"), "gap": external.get("gap"),
                         "audience": external.get("audience"), "demand_source": external.get("demand_source"),
                         "evidence_level": external.get("evidence_level"), "actionable": bool(external.get("actionable")),
                         "subscriber_fit": (external.get("scores") or {}).get("subscriber_fit_score")} if external else None})
    ranking.sort(key=lambda r: (r["opportunity_score"] is None, -(r["opportunity_score"] or 0), -(r["viewer_score"] or 0)))
    # Paid history and current paid state must remain auditable even where scores exist.
    for r in ranking:
        r["paid_note"] = (f"{r['paid'].get('paid_days_total', 0)} Werbetage in der Historie, zuletzt {r['paid'].get('last_paid_day')}" if r["paid"].get("paid_days_total") else "nie beworben")
    for i, r in enumerate(ranking):
        r["priority"] = i+1
    plan = daily_plan(ranking, today, record, recent_results(session))
    # Momentum ranking keeps `priority`; `active_rank` marks the active growth priority.
    statement = upsert(session, GrowthPlan).values(day=today, version=VERSION, created_at=now, plan=plan)
    session.execute(statement.on_conflict_do_update(index_elements=["day", "version"], set_={"plan": statement.excluded.plan, "created_at": statement.excluded.created_at}))
    session.commit()
    for entry in (plan.get("queue") or [])[:QUEUE_LIMIT]:
        external = entry.get("opportunity") or {}
        log.info("growth action rank=%s video=%r action=%s lever=%r metric=%s window=%s chance=%s/%s score=%s "
                 "audience=%r evidence=%s why=%r",
                 entry.get("rank"), entry.get("title"), entry.get("action"), entry.get("primary_lever"),
                 entry.get("target_metric"), entry.get("window_days"), external.get("kind"), external.get("gap"),
                 external.get("score"), entry.get("audience"), (entry.get("evidence") or {}).get("level"),
                 (entry.get("why") or "")[:200])
    log.info("growth plan day=%s queue=%s running=%s evaluated=%s note=%r", today, len(plan.get("queue") or []),
             len(plan.get("running_experiments") or []), evaluated, (plan.get("queue_note") or "")[:160])
    # Je Video nachvollziehbar, warum daraus heute eine Maßnahme wird oder nicht. Eine leere Queue ohne
    # diesen Grund ist von aussen nicht von einem Fehler zu unterscheiden.
    for row in (plan.get("ranking") or [])[:6]:
        chance = row.get("external") or {}
        log.info("growth ranking video=%r state=%s action=%s metric=%s eligible=%s reason=%r "
                 "chance=%s/%s score=%s level=%s usable=%s",
                 row.get("title"), row.get("state"), row.get("action"), row.get("target_metric"),
                 row.get("active_eligible"), (row.get("ineligible_reason") or "")[:120], chance.get("kind"),
                 chance.get("gap"), chance.get("score"), chance.get("evidence_level"), chance.get("context_usable"))
    for row in (plan.get("not_testable") or [])[:4]:
        log.info("growth not_measurable video=%r action=%s reason=%r", row.get("title"), row.get("action"),
                 (row.get("reason") or "")[:180])
    return {"evaluated_actions": evaluated, "ranked": len(ranking), "priority": plan.get("priority_video_id")}


PASSIVE_ACTIONS = ("protect_no_change", "observe")
INACTIVE_STATES = ("protect_momentum", "paid_excluded", "paid_cooldown", "insufficient_data")
NO_ACTIVE_ACTION = "Keine aktive Maßnahme empfohlen"


def active_priority_score(row):
    """Evidence-based *additional* growth potential of a changeable video: internal scores plus V6 external signals."""
    ext = row.get("external") or {}
    parts = [(row.get("opportunity_score"), 2.0), (row.get("viewer_score"), 1.0), (row.get("subscriber_score"), 1.0),
             (ext.get("score"), 2.0), (ext.get("subscriber_fit"), 1.0)]
    usable = [(v, w) for v, w in parts if v is not None]
    if not usable:
        return None
    return round(sum(v*w for v, w in usable)/sum(w for _, w in usable), 1)


def active_eligible(row):
    """Only organic, changeable videos with a concrete measure may carry active priority; protection is never overridden."""
    return row["state"] not in INACTIVE_STATES and row["action"] not in PASSIVE_ACTIONS


def queue_entry(row, rank, today):
    """One executable experiment, complete enough to act on without opening the code."""
    ext = row.get("external") or {}
    return {"rank": rank, "action_id": row.get("action_id"), "status": row.get("action_status") or PROPOSED,
            "video_id": row["video_id"], "title": row["title"], "state": row["state"], "action": row["action"],
            "objective": row["objective"], "audience": ext.get("audience") or (row.get("route") or {}).get("label"),
            "opportunity": {"kind": ext.get("kind"), "key": ext.get("key"), "gap": ext.get("gap"), "score": ext.get("score")} if ext else None,
            "why": row["reason"], "notes": row.get("notes") or [],
            "evidence": {"level": ext.get("evidence_level"), "demand_source": ext.get("demand_source"),
                         "families": ext.get("families") or [], "family_labels": ext.get("family_labels") or [],
                         "uncertainty": ext.get("uncertainty"), "actionable": bool(ext.get("actionable")),
                         "confidence": row.get("confidence"), "own_route": row.get("route"),
                         "missing": row.get("missing_evidence") or []},
            "baseline": row.get("baseline"), "steps": row.get("steps") or [],
            "primary_lever": row.get("primary_lever"), "deferred_levers": row.get("deferred_levers") or [],
            "requires": row.get("requires"), "choices": row.get("choices"),
            "needs_human_choice": bool(row.get("needs_human_choice")),
            "one_lever_note": "Genau ein veränderlicher Hebel; alles Weitere ist ein eigenes Experiment.",
            "context_status": ("belegt" if (ext.get("context_usable") if ext else False) else
                               "Hypothese – gibt keinen Wortlaut vor" if ext else "kein externer Kontext"),
            "context_reason": ext.get("context_reason") if ext else None,
            "discovery_surface": DISCOVERY_SURFACES.get(row["action"], "algorithmische YouTube-Flächen"),
            "reliability": row.get("reliability"), "reliability_note": row.get("reliability_note"),
            "expected_signal": f"{row['target_metric']} steigt messbar über die Wochenschwankung des Kanals",
            "target_metric": row["target_metric"], "window_days": row["window_days"],
            "measure_from": str(today), "evaluate_after": row["next_evaluation"],
            "success_criterion": row["success_criterion"], "stop_criterion": row.get("stop_criterion"),
            "do_not_change": row["do_not_change"], "executed_automatically": False,
            "confirm": {"required": True, "label": "Als durchgeführt markieren – Experiment starten",
                        "endpoint": f"/api/growth/actions/{row.get('action_id')}/start" if row.get("action_id") else None,
                        "effect": "Erst danach werden Baseline eingefroren, Startzeitpunkt gesetzt, das Messfenster gestartet "
                                  "und weitere Experimente für dieses Video gesperrt."},
            "note": "Vorschlag – noch nicht gestartet. Das System hat nichts auf YouTube geändert und kann es nicht "
                    "(Read-only-Zugriff); es zählt erst als laufend, wenn du die Durchführung bestätigst."}


def experiment_queue(ranking, today):
    """A short daily queue: at most one experiment per video, winners protected, running tests untouched."""
    queue, running, not_testable, rank = [], [], [], 0
    # Reihenfolge ausschliesslich nach Reichweiten-Aussicht: echter Growth-Hebel am eigenen Asset zuerst,
    # dann die Staerke der Evidenz, dann das geschaetzte Zusatzpotenzial. Nicht nach Messbarkeit.
    def growth_order(r):
        external = r.get("external") or {}
        return (0 if r["action"] in GROWTH_LEVERS else 1,
                -EVIDENCE_LEVEL_RANK.get(external.get("evidence_level"), 0),
                -(r["active_priority_score"] or 0), -(r["opportunity_score"] or 0))
    for row in sorted((r for r in ranking if r["active_eligible"]), key=growth_order):
        if row.get("action_status") == RUNNING:
            # Vom Menschen bestätigt gestartet und in Messung: sichtbar halten, nicht erneut anstoßen.
            running.append({"video_id": row["video_id"], "title": row["title"], "action": row["action"],
                            "action_id": row.get("action_id"), "started_day": row.get("started_day"),
                            "held_since": row.get("started_day") or row.get("held_since"),
                            "evaluate_after": row["next_evaluation"], "target_metric": row["target_metric"],
                            "note": "Läuft seit deiner Bestätigung – bis zur Auswertung nichts weiter an diesem Video ändern."})
            continue
        if row["action"] in EVIDENCE_ONLY:
            # Sicherheitsnetz: falls ein anderer Pfad so etwas waehlt, bleibt es intern.
            not_testable.append({"video_id": row["video_id"], "title": row["title"], "action": row["action"],
                                 "state": row["state"], "baseline": row.get("baseline"),
                                 "reason": ("Datenerzeugung ist keine Reichweiten-Maßnahme und erscheint nicht in "
                                            "JETZT TUN.")})
            continue
        ok, why = measurable(row.get("baseline"), row.get("target_metric"), row.get("action"))
        row["reliability"] = RELIABLE if ok else INDICATIVE
        row["reliability_note"] = why if not ok else None
        if not ok and strong_hypothesis(row.get("external_full") or row.get("external"), row["action"]):
            # Zulaessigkeit und Sicherheit sind zwei Fragen: die Hypothese ist belegt, die spaetere Aussage
            # bleibt indikativ. Beides wird benannt, nichts wird behauptet.
            ok = True
        if not ok:
            # Ohne messbare Ausgangsbasis waere jedes Ergebnis "unklar": sichtbar machen, aber nicht priorisieren.
            not_testable.append({"video_id": row["video_id"], "title": row["title"], "action": row["action"],
                                 "state": row["state"], "reason": why, "baseline": row.get("baseline")})
            continue
        if len(queue) >= QUEUE_LIMIT or any(q["video_id"] == row["video_id"] for q in queue):
            continue
        rank += 1
        queue.append(queue_entry(row, rank, today))
    return queue, running, not_testable


def daily_plan(ranking, today, record, results=None):
    """Two separate concepts: the momentum/performance ranking (protection stays visible at the top) and the
    active growth priority – the changeable video with the best evidence for additional organic growth."""
    for r in ranking:
        r["momentum_rank"] = r["priority"]
        r["active_eligible"] = active_eligible(r)
        r["active_priority_score"] = active_priority_score(r) if r["active_eligible"] else None
        r["active_rank"] = None
        r["ineligible_reason"] = (None if r["active_eligible"] else
            "Momentum wird geschützt – bewusst keine Änderung" if r["state"] == "protect_momentum" else
            "Werbetraffic: keine organische Optimierungspriorität" if r["state"] in ("paid_excluded", "paid_cooldown") else
            "Zu wenig Daten" if r["state"] == "insufficient_data" else
            "Nur Beobachtung empfohlen – keine aktive Maßnahme" if r["action"] == "observe" else "Kein belastbarer Score")
    candidates = sorted((r for r in ranking if r["active_eligible"]), key=lambda r: (-(r["active_priority_score"] or 0), -(r["opportunity_score"] or 0)))
    for i, r in enumerate(candidates):
        r["active_rank"] = i+1
    protected = [{"video_id": r["video_id"], "title": r["title"], "regime": r["regime"], "action": r["action"], "reason": r["reason"],
                  "opportunity_score": r["opportunity_score"], "do_not_change": r["do_not_change"], "momentum_rank": r["momentum_rank"]}
                 for r in ranking if r["state"] == "protect_momentum"]
    momentum_top = ranking[0] if ranking else None
    subscriber = max((r for r in ranking if r["subscriber_score"] is not None), key=lambda r: r["subscriber_score"], default=None)
    viewer = max((r for r in ranking if r["viewer_score"] is not None), key=lambda r: r["viewer_score"], default=None)
    queue, running, not_testable = experiment_queue(ranking, today)
    base = {"day": str(today), "version": VERSION, "ranking": ranking, "momentum_ranking": [r["video_id"] for r in ranking],
            "protected": protected, "track_record": record, "note": SCORE_NOTE+" "+GENERALIZATION_NOTE,
            "read_only": "Keine automatischen Änderungen auf YouTube.",
            "queue": queue, "queue_limit": QUEUE_LIMIT, "running_experiments": running, "results": results or [],
            "not_testable": not_testable,
            "now_do": queue[0] if queue else None,
            "queue_note": ("Ausführbare Experimente für heute – von dir auszuführen, das System ändert nichts auf YouTube. "
                           f"Höchstens {QUEUE_LIMIT} gleichzeitig und nie zwei am selben Video."
                           if queue else "Heute keine datenbegründete Reichweiten-Maßnahme: geschützte oder laufende "
                                        "Videos, oder für kein Video liegt eine belegte Audience-/Placement-Chance vor."),
            "momentum_top": {"video_id": momentum_top["video_id"], "title": momentum_top["title"], "state": momentum_top["state"],
                             "opportunity_score": momentum_top["opportunity_score"]} if momentum_top else None,
            "subscriber_focus": subscriber["video_id"] if subscriber else None, "viewer_focus": viewer["video_id"] if viewer else None,
            "priority_semantics": "Aktive Growth-Priorität = änderbares, organisches Video mit der besten evidenzbasierten zusätzlichen Chance; "
                                  "geschützte Videos behalten ihren Schutz und erscheinen separat."}
    if momentum_top is None:
        return {**base, "status": "insufficient_data", "active_status": "none", "priority_video_id": None, "priority_title": None,
                "action": None, "why": "Keine Videos bewertet.", "why_priority": NO_ACTIVE_ACTION, "objective": None, "do_not_change": [],
                "success_metric": None, "success_criterion": None, "window_days": None, "next_evaluation": None, "confidence": "insufficient_data",
                "internal_signals": None, "external_signals": {"available": False}, "combined_decision": NO_ACTIVE_ACTION+"."}
    top = candidates[0] if candidates else None
    if top is None:
        reasons = "; ".join(f"{r['title']}: {r['ineligible_reason']}" for r in ranking)
        status = "insufficient_data" if all(r["state"] == "insufficient_data" for r in ranking) else "no_active_action"
        return {**base, "status": status, "active_status": "none", "priority_video_id": None, "priority_title": None,
                "action": None, "why": reasons, "why_priority": NO_ACTIVE_ACTION+" – kein änderbares Video mit ausreichender Evidenz.",
                "objective": None, "do_not_change": sorted({d for r in ranking for d in r["do_not_change"]}), "success_metric": None,
                "success_criterion": None, "window_days": None, "next_evaluation": None,
                "confidence": min((r["confidence"] for r in ranking), key=lambda c: {"insufficient_data": 0, "low": 1, "moderate": 2}.get(c, 0)),
                "internal_signals": None, "external_signals": {"available": False, "note": "Keine externe Chance lenkt derzeit eine aktive Maßnahme."},
                "combined_decision": NO_ACTIVE_ACTION+"; "+("Schutz aktiv für: "+", ".join(p["title"] for p in protected)+"." if protected else "beobachten und Daten sammeln.")}
    objective = top["objective"]
    if subscriber and subscriber["video_id"] == top["video_id"] and top["action"] == "cross_promote" \
            and (top["subscriber_score"] or 0) > (top["viewer_score"] or 0):
        objective = "Subscriber"
    ext = top.get("external")
    internal = {"state": top["state"], "regime": top["regime"], "breakout": top["breakout"], "opportunity_score": top["opportunity_score"],
                "viewer_score": top["viewer_score"], "subscriber_score": top["subscriber_score"], "paid_status": top.get("paid_status"),
                "momentum_v2": (top.get("momentum") or {}).get("score"), "momentum_rank": top["momentum_rank"],
                "active_priority_score": top["active_priority_score"]}
    external_block = {"available": bool(ext), "score": ext.get("score") if ext else None, "kind": ext.get("kind") if ext else None,
                      "key": ext.get("key") if ext else None, "gap": ext.get("gap") if ext else None, "audience": ext.get("audience") if ext else None,
                      "demand_source": ext.get("demand_source") if ext else None, "subscriber_fit": ext.get("subscriber_fit") if ext else None,
                      "evidence_level": ext.get("evidence_level") if ext else None, "actionable": bool(ext and ext.get("actionable")),
                      "note": "Externe Nachfrage-Signale sind Proxies, außer sie stammen aus eigenen Analytics."}
    combined = (f"Aktive Growth-Priorität #1: {top['title']} – interner Zustand {top['state']}"
                + (f" + externe Chance „{ext.get('key')}“ ({ext.get('demand_source') or 'Herkunft unbekannt'}, "
                   f"{'Thema belegt' if ext.get('context_usable') else 'Thema unbelegt: nur Hypothese'})"
                   if ext else " ohne externe Chance") + f" → {top['action']}."
                + (" Geschützt (keine Änderung): "+", ".join(p["title"] for p in protected)+"." if protected else ""))
    return {**base, "status": "ok", "active_status": "active", "priority_video_id": top["video_id"], "priority_title": top["title"],
            "why": top["reason"],
            "why_priority": f"Höchste aktive Growth-Priorität ({top['active_priority_score']}) unter den änderbaren organischen Videos; "
                            f"Zustand {top['state']}, Momentum-Rang {top['momentum_rank']}."
                            + (f" Höherer Momentum-Score bei {momentum_top['title']} ({momentum_top['opportunity_score']}) bleibt geschützt." if momentum_top["video_id"] != top["video_id"] and momentum_top["state"] == "protect_momentum" else ""),
            "internal_signals": internal, "external_signals": external_block, "combined_decision": combined,
            "action": top["action"], "objective": objective, "do_not_change": top["do_not_change"], "success_metric": top["target_metric"],
            "success_criterion": top["success_criterion"], "window_days": top["window_days"], "next_evaluation": top["next_evaluation"],
            "confidence": top["confidence"]}


def live_paid_profiles(session, now=None):
    """Paid status recomputed from current data; a stored profile can be days behind."""
    from .history import load as load_histories
    today = pacific_day(now or utcnow())
    return {h.video.id: paid_profile(h, today) for h in load_histories(session)}


def reconcile_plan(session, plan, titles):
    """The stored plan is a snapshot; the status of its actions is live.

    Without this the dashboard keeps showing "Vorschlag – noch nicht gestartet" after a confirmation,
    because only the next hourly run rewrites the snapshot. The queue must never contradict the database.
    """
    if not plan:
        return plan
    plan = deepcopy(plan)
    rows = list(session.scalars(select(GrowthAction).where(GrowthAction.status.in_([PROPOSED, RUNNING]),
                                                           GrowthAction.version == VERSION)))
    by_id = {r.id: r for r in rows}
    open_by_video = {}
    for row in rows:
        if row.status != PROPOSED:
            continue
        current = open_by_video.get(row.video_id)
        if current is None or (row.created_day, row.id) > (current.created_day, current.id):
            open_by_video[row.video_id] = row
    queue, notes = [], []
    for entry in plan.get("queue") or []:
        row = by_id.get(entry.get("action_id"))
        if row is None or row.status != PROPOSED:
            live = open_by_video.get(entry.get("video_id"))
            if live is None:
                # Gestartet, ausgewertet oder ersetzt: keine offene Aufgabe mehr.
                notes.append(f"{entry.get('title')}: nicht mehr offen (Status live abgeglichen).")
                continue
            entry = {**entry, "action_id": live.id, "status": live.status}
        queue.append(entry)
    for rank, entry in enumerate(queue, 1):
        entry["rank"] = rank
        confirm = entry.get("confirm") or {}
        entry["confirm"] = {**confirm, "endpoint": f"/api/growth/actions/{entry['action_id']}/start"}
    running = [{"video_id": r.video_id, "title": titles.get(r.video_id, r.video_id), "action": r.action,
                "action_id": r.id, "started_day": str(r.started_day) if r.started_day else None,
                "held_since": str(r.started_day) if r.started_day else None,
                "evaluate_after": str(r.evaluate_after), "target_metric": r.target_metric,
                "baseline": r.baseline or {},
                "note": "Läuft seit deiner Bestätigung – bis zur Auswertung nichts weiter an diesem Video ändern."}
               for r in sorted((x for x in rows if x.status == RUNNING), key=lambda x: (x.started_day or x.created_day, x.id))]
    plan["queue"], plan["running_experiments"] = queue, running
    plan["now_do"] = queue[0] if queue else None
    plan["live_status_note"] = ("Status der Maßnahmen live aus der Datenbank abgeglichen; der Plan selbst ist die "
                               "Momentaufnahme des letzten Laufs." if not notes else
                               "Status live abgeglichen: "+" ".join(notes))
    if not queue and running:
        plan["queue_note"] = ("Alle offenen Vorschläge sind bestätigt und laufen. Bis zur Auswertung nichts weiter "
                              "an diesen Videos ändern.")
    return plan


def overview(session, now=None):
    try:
        live = live_paid_profiles(session, now)
    except Exception:
        # Der Lesepfad darf nie am Neuberechnen scheitern; gespeicherte Werte bleiben sichtbar.
        live = {}
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
                                         "viewer": row.viewer, "subscriber": row.subscriber, "revival": row.revival, "momentum": row.momentum,
                                         "paid_live": live.get(video.id), "paid_stored": (row.momentum or {}).get("paid")}
    titles = {v.id: v.title for v in session.scalars(select(Video))}
    return {"version": VERSION, "plan": reconcile_plan(session, plan.plan if plan else None, titles),
            "plan_day": plan.day if plan else None,
            "paid_live": live,
            "scores": scores_by_video, "actions": {k: v[:10] for k, v in actions.items()}, "track_record": track_record(session),
            "states": STATES, "actions_catalog": ACTIONS, "read_only": True}
