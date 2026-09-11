"""Transparent V1 heuristics; descriptive factors are not causal explanations."""
from datetime import timezone, timedelta
from math import log2, tanh
from statistics import mean


def aware(t):
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def velocity(snapshots):
    points = sorted(snapshots, key=lambda p: aware(p.observed_at))
    if len(points) < 3:
        return dict(velocity=None, acceleration=None, quality="Mindestens 3 Snapshots erforderlich")
    end = points[-1]
    # Choose two comparable 24h windows. Never treat a long outage as 24 hours.
    def nearest(target, candidates):
        return min(candidates, key=lambda p: abs((aware(p.observed_at) - target).total_seconds()), default=None)
    middle = nearest(aware(end.observed_at) - timedelta(hours=24), points[:-1])
    early = nearest(aware(middle.observed_at) - timedelta(hours=24),
                    [p for p in points if aware(p.observed_at) < aware(middle.observed_at)])
    if early is None:
        return dict(velocity=None, acceleration=None, quality="Noch keine zwei Vergleichsfenster")
    h1 = (aware(middle.observed_at) - aware(early.observed_at)).total_seconds() / 3600
    h2 = (aware(end.observed_at) - aware(middle.observed_at)).total_seconds() / 3600
    if min(h1, h2) < 1 or max(h1, h2) > 36:
        return dict(velocity=None, acceleration=None, quality="Snapshot-Abstand ungeeignet")
    # Check every interval, not just window endpoints, for counter corrections.
    selected = [p for p in points if aware(p.observed_at) >= aware(early.observed_at)]
    if any(b.views < a.views for a, b in zip(selected, selected[1:])):
        return dict(velocity=None, acceleration=None, quality="Zählerkorrektur erkannt")
    old, current = (middle.views - early.views) / h1, (end.views - middle.views) / h2
    return dict(velocity=current, acceleration=(current-old)/((h1+h2)/2),
                previous_velocity=old, quality="ok", window_hours=h2)


def features(video, snapshots, daily):
    result = velocity(snapshots)
    views = sum(d.views for d in daily)
    result.update(
        views=snapshots[-1].views if snapshots else None,
        subscriber_conversion=sum(d.subscribers_gained for d in daily)/views if views else None,
        watchtime_efficiency=sum(d.watch_minutes for d in daily)*60/views/video.duration_seconds
            if views and video.duration_seconds else None,
        watch_minutes=sum(d.watch_minutes for d in daily),
        subscribers_gained=sum(d.subscribers_gained for d in daily),
        subscribers_net=sum(d.subscribers_gained-d.subscribers_lost for d in daily),
        analytics_views=views,
        analytics_days=len(daily),
        content_type=daily[-1].content_type if daily else "UNKNOWN",
    )
    return result


def momentum(current, peers):
    usable = [p for p in peers if p.get("velocity") is not None]
    if current["velocity"] is None:
        return dict(score=None, relative_performance=None, direction="Daten fehlen",
                    reasons=[current["quality"]], actions=["Regelmäßige Snapshots sammeln."])
    baseline = mean(p["velocity"] for p in usable) if usable else None
    ratio = current["velocity"]/baseline if baseline and baseline > 0 else None
    old = current.get("previous_velocity", 0)
    trend = (current["velocity"]-old)/max(1, old)
    parts = [(0.65, tanh(trend))]
    reasons = [f"Views pro Stunde: {current['velocity']:.1f}; vorher {old:.1f}."]
    if ratio is not None:
        parts.append((0.35, tanh(log2(max(ratio, 0.01)))))
        reasons.append(f"{ratio:.2f}× Durchschnitt der vergleichbaren Kanalvideos.")
    score = 50 + 50*sum(w*x for w,x in parts)/sum(w for w,x in parts)
    actions = ["Traffic-Quellen und Retention prüfen; Veränderungen als Hypothesen behandeln."]
    retention = current["watchtime_efficiency"]
    if retention is not None and retention < 0.35:
        actions.append("Frühe Retention-Abfälle ansehen und den Einstieg des nächsten Videos gezielt testen.")
    if trend > 0.15:
        actions.append("Thematisch passendes Folgevideo und hilfreiche Endscreen-Verknüpfung prüfen.")
    elif trend < -0.15:
        actions.append("Reichweite und CTR getrennt prüfen, bevor Titel oder Thumbnail verändert werden.")
    return dict(score=round(score, 1), relative_performance=ratio,
                direction="Gewinnt" if trend > 0.15 else "Verliert" if trend < -0.15 else "Stabil",
                reasons=reasons, actions=actions)
