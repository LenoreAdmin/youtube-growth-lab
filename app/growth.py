"""Versioned descriptive growth signals; no causal or calibrated probability claim."""
from datetime import timedelta
from math import log1p, tanh
from statistics import median
from .metrics import aware

VERSION = "growth-v2"
WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}


def window(points, hours, end=None):
    # Stable deduplication; disagreeing counters at one timestamp are ambiguous.
    grouped = {}
    for p in points:
        t = aware(p.observed_at)
        if end is None or t <= aware(end):
            grouped.setdefault(t, set()).add(p.views)
    rows = sorted(grouped)
    empty = dict(velocity=None, acceleration=None, delta_views=None, previous_velocity=None,
                 requested_hours=hours, actual_hours=None, quality="insufficient_history")
    if not rows:
        return empty
    finish = aware(end) if end else rows[-1]
    tolerance = min(hours * .2, 2)
    def interval(stop):
        target = stop-timedelta(hours=hours)
        last = min(rows, key=lambda t: abs((t-stop).total_seconds()))
        first = min(rows, key=lambda t: abs((t-target).total_seconds()))
        if abs((last-stop).total_seconds()) > tolerance*3600 or abs((first-target).total_seconds()) > tolerance*3600 or last <= first:
            return None, "insufficient_history"
        selected = [t for t in rows if first <= t <= last]
        if any(len(grouped[t]) != 1 for t in selected):
            return None, "conflicting_snapshots"
        counts = [next(iter(grouped[t])) for t in selected]
        if any(b < a for a,b in zip(counts, counts[1:])):
            return None, "counter_correction"
        if any((b-a).total_seconds()/3600 > max(3, min(36, hours*.15)) for a,b in zip(selected, selected[1:])):
            return None, "snapshot_gap"
        duration = (last-first).total_seconds()/3600
        return (counts[-1]-counts[0], duration), "ok"
    current, quality = interval(finish)
    if current is None:
        return {**empty, "quality": quality}
    gain, duration = current
    previous, previous_quality = interval(finish-timedelta(hours=hours))
    old = previous[0]/previous[1] if previous else None
    speed = gain/duration
    return {**empty, "velocity": speed, "delta_views": gain, "actual_hours": duration,
            "previous_velocity": old, "acceleration": (speed-old)/((duration+previous[1])/2) if previous else None,
            "quality": "ok", "previous_quality": previous_quality}


def enrich(features, snapshots, now):
    windows = {label: window(snapshots, hours) for label,hours in WINDOWS.items()}
    stale = not snapshots or (aware(now)-max(aware(p.observed_at) for p in snapshots)).total_seconds() > 3*3600
    if stale:
        for value in windows.values():
            value.update(velocity=None, acceleration=None, quality="stale")
    daily = windows["24h"]
    # Independent trailing daily velocities. Current 24h is excluded completely.
    history = []
    if snapshots:
        end = max(aware(p.observed_at) for p in snapshots)
        for day in range(1, 31):
            prior = window(snapshots, 24, end-timedelta(days=day))
            if prior["velocity"] is not None:
                history.append(prior["velocity"])
    return {**features, **{key:daily[key] for key in ("velocity","acceleration","previous_velocity","quality")},
            "windows": windows, "historical_velocities": history, "growth_version": VERSION}


def assess(current, peers):
    speed = current.get("velocity")
    history = current.get("historical_velocities", [])
    reference = history if len(history) >= 7 else [p["velocity"] for p in peers if p.get("velocity") is not None]
    source = "own_nonoverlapping_days" if len(history) >= 7 else "peer_videos"
    baseline = median(reference) if reference else None
    z = None
    if baseline is not None and speed is not None:
        logs = [log1p(max(0,x)) for x in reference]
        center = median(logs)
        scale = max(.15, 1.4826*median(abs(x-center) for x in logs))
        z = (log1p(speed)-center)/scale
    parts = {}
    if z is not None:
        parts["velocity"] = (.35, tanh(z/3))
    old = current.get("previous_velocity")
    trend = (speed-old)/max(1,old) if speed is not None and old is not None else None
    if trend is not None:
        parts["acceleration"] = (.25, tanh(trend))
    for key, weight in (("ctr",.15),("watchtime_efficiency",.15),("subscriber_conversion",.10)):
        value = current.get(key)
        comparable = [p[key] for p in peers if p.get(key) is not None]
        if value is not None and comparable:
            center = median(comparable)
            if center > 0:
                parts[key] = (weight, tanh((value-center)/center))
    valid = speed is not None and not (current.get("advertising_views_reported") or 0)
    score = round(50+50*sum(w*v for w,v in parts.values())/sum(w for w,v in parts.values()),1) if valid and parts else None
    regime = "unknown"
    if valid:
        regime = "cooling" if trend is not None and trend < -.15 else "rising" if trend is not None and trend > .15 else "baseline"
        if z is not None and len(reference) >= 7 and z >= 3 and trend is not None and trend > .15:
            regime = "breakout"
    reasons = [f"24h velocity: {speed:.2f} views/hour" if speed is not None else "24h window unavailable: "+current.get("quality","unknown")]
    if baseline is not None:
        reasons.append(f"Robust baseline: {baseline:.2f} views/hour; n={len(reference)} ({source}).")
    reasons.append("Available factors: "+(", ".join(parts) or "none")+"; missing factors are excluded, not zeroed.")
    if not valid and speed is not None:
        reasons.append("Advertising detected: organic score and breakout classification suspended.")
    return dict(score=score, regime=regime, direction="Gewinnt" if regime in ("rising","breakout") else "Verliert" if regime=="cooling" else "Stabil" if valid else "Daten fehlen",
                relative_performance=speed/baseline if valid and baseline else None,
                baseline={"velocity":baseline,"n":len(reference),"source":source,"robust_z":z},
                factors={k:{"weight":w,"signal":v} for k,(w,v) in parts.items()},
                evidence_quality="descriptive" if len(reference)>=7 else "low_sample",
                reasons=reasons, actions=["Traffic und Reichweite prüfen; eine Änderung als Experiment vorregistrieren.",
                    "Erfolge und Fehlschläge gemeinsam auswerten; ein Breakout beweist keine Strategie."])
