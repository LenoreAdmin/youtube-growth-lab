"""Operational health of the background jobs. A saturated sync must never look healthy.

Read-only checks over existing tables: a stale growth plan, a stale discovery run or an
action that stayed pending past its evaluation date are reported as explicit warnings so
a silent ten-day standstill cannot happen again.
"""
from datetime import timedelta
from sqlalchemy import select
from .models import SyncRun, GrowthPlan, GrowthAction, DiscoveryRun, LearningDataset, utcnow
from .metrics import aware
from .history import pacific_day

STALE_WARN_HOURS = 24
STALE_CRITICAL_HOURS = 48
DEFERRED_STREAK = 3
LEVELS = ("ok", "warn", "critical")


def _age_hours(now, moment):
    return None if moment is None else round((aware(now)-aware(moment)).total_seconds()/3600, 1)


def health(session, now=None):
    now = now or utcnow()
    today = pacific_day(now)
    runs = list(session.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(12)))
    streak = 0
    for run in runs:
        if run.status in ("deferred", "failed", "partial"):
            streak += 1
        else:
            break
    plan = session.scalar(select(GrowthPlan).order_by(GrowthPlan.day.desc(), GrowthPlan.id.desc()))
    discovery = session.scalar(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()))
    dataset = session.scalar(select(LearningDataset).order_by(LearningDataset.built_at.desc()))
    # Nur ein bestaetigt gestartetes Experiment kann ueberfaellig sein; ein Vorschlag laeuft nicht.
    overdue = [r for r in session.scalars(select(GrowthAction).where(GrowthAction.status == "running"))
               if r.evaluate_after < today]
    checks = {
        "last_sync_status": runs[0].status if runs else None,
        "last_sync_finished_at": runs[0].finished_at if runs else None,
        "deferred_streak": streak,
        "plan_age_hours": _age_hours(now, plan.created_at if plan else None),
        "plan_day": str(plan.day) if plan else None,
        "discovery_age_hours": _age_hours(now, (discovery.finished_at or discovery.started_at) if discovery else None),
        "discovery_status": discovery.status if discovery else None,
        "dataset_age_hours": _age_hours(now, dataset.built_at if dataset else None),
        "overdue_actions": [{"video_id": r.video_id, "action": r.action, "created_day": str(r.created_day),
                             "evaluate_after": str(r.evaluate_after), "days_overdue": (today-r.evaluate_after).days}
                            for r in overdue],
        "thresholds": {"stale_warn_hours": STALE_WARN_HOURS, "stale_critical_hours": STALE_CRITICAL_HOURS,
                       "deferred_streak": DEFERRED_STREAK},
    }
    warnings, level = [], "ok"

    def raise_level(new):
        nonlocal level
        if LEVELS.index(new) > LEVELS.index(level):
            level = new

    for label, key in (("Growth Plan", "plan_age_hours"), ("Discovery-Lauf", "discovery_age_hours"),
                       ("Learning-Datensatz", "dataset_age_hours")):
        age = checks[key]
        if age is None:
            warnings.append(f"{label}: noch nie erzeugt.")
            raise_level("critical")
        elif age > STALE_CRITICAL_HOURS:
            warnings.append(f"{label} ist {age:.0f} h alt (Grenze {STALE_CRITICAL_HOURS} h) – Jobs laufen nicht durch.")
            raise_level("critical")
        elif age > STALE_WARN_HOURS:
            warnings.append(f"{label} ist {age:.0f} h alt.")
            raise_level("warn")
    if overdue:
        worst = max(o["days_overdue"] for o in checks["overdue_actions"])
        warnings.append(f"{len(overdue)} Aktion(en) stehen {worst} Tag(e) über ihren Auswertungstermin hinaus auf pending.")
        raise_level("critical")
    if streak >= DEFERRED_STREAK:
        warnings.append(f"{streak} Syncs in Folge ohne Status ok (deferred/partial/failed) – Zeitbudget oder Fehlerquelle prüfen.")
        raise_level("critical")
    elif checks["last_sync_status"] and checks["last_sync_status"] != "ok":
        warnings.append(f"Letzter Sync endete mit Status {checks['last_sync_status']} – nicht als gesund werten.")
        raise_level("warn")
    if checks["discovery_status"] in ("failed", "partial"):
        warnings.append(f"Letzter Discovery-Lauf: {checks['discovery_status']}.")
        raise_level("warn")
    return {"level": level, "warnings": warnings, "checks": checks,
            "note": "deferred ist kein gesunder Zustand: gespeicherter Fortschritt, aber unvollständiger Lauf."}
