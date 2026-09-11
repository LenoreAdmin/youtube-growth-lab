"""Preregistered strategy memory; matched controls are quasi-experiments, not randomized proof."""
import hashlib
import json
from datetime import timedelta
from typing import Literal
from pydantic import BaseModel, Field
from sqlalchemy import select
from scipy.stats import beta
from .models import Decision, Video, Snapshot, Forecast, utcnow
from .metrics import aware

STRATEGIES = ("content_strategy", "title_strategy", "thumbnail_strategy", "hook_strategy", "audience")


class DecisionInput(BaseModel):
    hypothesis: str = Field(min_length=8, max_length=2000)
    content_strategy: str = Field(min_length=2, max_length=1000)
    title_strategy: str = Field(min_length=2, max_length=1000)
    thumbnail_strategy: str = Field(min_length=2, max_length=1000)
    hook_strategy: str = Field(min_length=2, max_length=1000)
    audience: str = Field(min_length=2, max_length=1000)
    expected_views_gain: int = Field(ge=0)
    horizon_hours: Literal[24, 168, 720] = 168
    design: Literal["observational", "matched_control"] = "observational"
    control_video_id: str | None = None
    confounders: str = Field(default="", max_length=2000)


def create_decision(session, data):
    values = data.model_dump()
    key = hashlib.sha256(json.dumps([values[k].strip().lower() for k in STRATEGIES]).encode()).hexdigest()
    row = Decision(**{k: values[k] for k in STRATEGIES},
        hypothesis=data.hypothesis, strategy_key=key, design=data.design,
        control_video_id=data.control_video_id, confounders=data.confounders,
        expected_result={"metric": "additional_views", "value": data.expected_views_gain, "horizon_hours": data.horizon_hours})
    if data.design == "matched_control" and not session.get(Video, data.control_video_id):
        raise ValueError("Vergleichsvideo fehlt.")
    session.add(row)
    session.flush()
    return row


def activate(session, decision, video_id, now=None):
    now = now or utcnow()
    if decision.status != "draft":
        raise ValueError("Eine registrierte Hypothese ist unveränderlich.")
    video = session.get(Video, video_id)
    if not video:
        raise ValueError("Video zuerst synchronisieren.")
    running = session.scalar(select(Decision.id).where(Decision.video_id == video_id, Decision.status == "registered"))
    if running:
        raise ValueError("Bereits ein laufendes Experiment für dieses Video.")
    snap = session.scalar(select(Snapshot).where(Snapshot.video_id == video_id).order_by(Snapshot.observed_at.desc()))
    if snap is None or (aware(now)-aware(snap.observed_at)).total_seconds() > 7200:
        raise ValueError("Aktueller Snapshot erforderlich.")
    h = decision.expected_result["horizon_hours"]
    measurement = {"origin_at": aware(snap.observed_at).isoformat(), "origin_views": snap.views,
                   "target_at": (aware(snap.observed_at)+timedelta(hours=h)).isoformat()}
    if decision.design == "matched_control":
        control = session.get(Video, decision.control_video_id)
        if not control or control.id == video_id or control.channel_id != video.channel_id:
            raise ValueError("Anderes Vergleichsvideo desselben Kanals erforderlich.")
        from .pipeline import latest_features
        f = {v.id: values for v, _, values in latest_features(session, now)}
        a, b = f.get(video_id), f.get(control.id)
        if not a or not b or a["content_type"] == "UNKNOWN" or a["content_type"] != b["content_type"]:
            raise ValueError("Vergleich erfordert denselben bekannten Content-Typ.")
        if min(a["age_days"]//30, 3) != min(b["age_days"]//30, 3):
            raise ValueError("Vergleich erfordert eine passende Alterskohorte.")
        if not decision.confounders.strip():
            raise ValueError("Mögliche Störfaktoren vorab dokumentieren.")
        c = session.scalar(select(Snapshot).where(Snapshot.video_id == control.id).order_by(Snapshot.observed_at.desc()))
        cp = session.scalar(select(Forecast).where(Forecast.video_id == control.id, Forecast.horizon_hours == h)
                            .order_by(Forecast.origin_at.desc()))
        if c is None or cp is None or abs((aware(c.observed_at)-aware(snap.observed_at)).total_seconds()) > 7200:
            raise ValueError("Zeitnaher Vergleichs-Snapshot und Kontrollprognose erforderlich.")
        if abs((aware(cp.origin_at)-aware(c.observed_at)).total_seconds()) > 7200:
            raise ValueError("Kontrollprognose veraltet.")
        measurement.update(control_origin_views=c.views,
            control_origin_at=aware(c.observed_at).isoformat(),
            control_target_at=(aware(c.observed_at)+timedelta(hours=h)).isoformat(),
            control_expected_gain=max(0, cp.predicted_views-cp.origin_views))
    decision.video_id, decision.status, decision.measurement = video_id, "registered", measurement


def evidence(session, key, before=None):
    query = select(Decision).where(Decision.strategy_key == key, Decision.status == "evaluated")
    if before:
        query = query.where(Decision.evaluated_at < before)
    rows = list(session.scalars(query.order_by(Decision.evaluated_at)))
    # Each video contributes once. A reused control contributes once to controlled evidence.
    seen_videos, seen_controls, wins, failures, observations = set(), set(), 0, 0, 0
    for row in rows:
        if row.video_id in seen_videos or row.video_id in seen_controls:
            continue
        seen_videos.add(row.video_id)
        observations += 1
        if row.design != "matched_control" or row.control_video_id in seen_controls or row.control_video_id in seen_videos:
            continue
        seen_controls.add(row.control_video_id)
        lift = row.actual_result.get("control_adjusted_residual")
        if lift is None:
            continue
        wins += int(lift > 0)
        failures += int(lift <= 0)
    n = wins+failures
    confidence = float(beta.sf(0.5, 1+wins, 1+failures))
    lower, upper = (float(x) for x in beta.ppf([0.025, 0.975], 1+wins, 1+failures))
    return {"n": n, "wins": wins, "failures": failures, "observations": observations,
            "confidence": confidence, "success_probability_interval": [lower, upper],
            "status": "Wiederholt vielversprechend, nicht kausal bewiesen" if n >= 10 and lower > 0.5
                      else "Evidenz gegen Strategie" if n >= 10 and upper < 0.5 else "Unentschieden",
            "model_feature": ((wins+1)/(n+2)*2-1)*n/(n+10)}


def evaluate_memory(session, now):
    from datetime import datetime
    for row in session.scalars(select(Decision).where(Decision.status == "registered")):
        m = row.measurement
        target = datetime.fromisoformat(m["target_at"])
        if target > aware(now):
            continue
        def outcome(video_id, when):
            return session.scalar(select(Snapshot).where(Snapshot.video_id == video_id,
                Snapshot.observed_at >= when, Snapshot.observed_at <= now, Snapshot.observed_at <= when+timedelta(hours=2))
                .order_by(Snapshot.observed_at))
        actual = outcome(row.video_id, target)
        if actual is None or actual.views < m["origin_views"]:
            continue
        gain = actual.views-m["origin_views"]
        expected = row.expected_result["value"]
        residual = (gain-expected)/max(1, expected)
        result = {"views_gain": gain, "views": actual.views, "observed_at": aware(actual.observed_at).isoformat(),
                  "relative_deviation": residual, "control_adjusted_residual": None}
        if row.design == "matched_control":
            control = outcome(row.control_video_id, datetime.fromisoformat(m["control_target_at"]))
            if control is None or control.views < m["control_origin_views"]:
                continue
            control_gain = control.views-m["control_origin_views"]
            control_residual = (control_gain-m["control_expected_gain"])/max(1, m["control_expected_gain"])
            result.update(control_gain=control_gain, control_adjusted_residual=residual-control_residual)
        row.actual_result, row.deviation = result, gain-expected
        row.status, row.evaluated_at = "evaluated", now
        row.suspected_cause = "Noch nicht geprüft; Traffic, Retention und Störfaktoren vergleichen."
        row.next_hypothesis = (
            "Erfolg replizieren: dieselbe Strategie auf einem unabhängigen Video gegen einen vorab definierten Vergleich testen."
            if residual > 0 else
            "Fehlschlag untersuchen: Retention und Traffic prüfen, dann genau eine Strategiekomponente mit neuer Zielprognose testen."
        )
        session.flush()
        row.confidence_score = evidence(session, row.strategy_key)["confidence"]


def strategy_feature(session, video_id, before):
    row = session.scalar(select(Decision).where(Decision.video_id == video_id, Decision.created_at <= before,
        Decision.status.in_(["registered", "evaluated"])).order_by(Decision.created_at.desc()))
    return evidence(session, row.strategy_key, before)["model_feature"] if row else 0.0
