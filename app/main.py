import secrets
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, AwareDatetime
from typing import Literal
from sqlalchemy import select, text
from .db import Session
from .config import settings
from .models import Channel, Video, Snapshot, Daily, Reach, Report, Forecast, SyncRun, Decision, MemoryReview, GrowthAssessment, ExperimentChange, PredictionAudit, ModelRun, utcnow
from .pipeline import dashboard_rows, collect
from .memory import DecisionInput, create_decision, activate, evidence
from . import backfill as backfill_module
from . import learning as learning_module
from . import growth_engine as growth_module
from . import discovery as discovery_module

app = FastAPI(title="YouTube Growth Lab", version="0.1.0")
security = HTTPBearer(auto_error=False)
static = Path(__file__).parent/"static"


def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    if not settings.app_token or settings.app_token.startswith("REPLACE_"):
        raise HTTPException(503, "APP_TOKEN muss lokal konfiguriert werden.")
    if not credentials or not secrets.compare_digest(credentials.credentials.encode(), settings.app_token.encode()):
        raise HTTPException(401, "Anmeldung erforderlich.")


def db():
    with Session() as s:
        yield s


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response


@app.get("/")
def index():
    return FileResponse(static/"index.html")


@app.get("/health")
def health(s=Depends(db)):
    s.execute(text("SELECT 1"))
    return {"status": "ok"}


def cron_authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    secret = settings.cron_secret
    if len(secret) < 32 or secret.startswith("REPLACE_"):
        raise HTTPException(503, "Cron secret is not configured.")
    if not credentials or not secrets.compare_digest(credentials.credentials.encode(), secret.encode()):
        raise HTTPException(401, "Invalid cron authorization.")


@app.get("/api/cron/sync", dependencies=[Depends(cron_authenticate)], include_in_schema=False)
def cron_sync():
    if settings.vercel_env == "preview":
        raise HTTPException(403, "Sync is disabled in preview deployments.")
    bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    try:
        result = collect(bucket=bucket)
    except Exception:
        return JSONResponse({"status": "failed", "detail": "Sync unavailable; check server configuration."}, status_code=503)
    code = 503 if result["status"] in ("failed", "partial") else 200
    return JSONResponse(result, status_code=code)


@app.post("/api/sync", dependencies=[Depends(authenticate)])
def manual_sync():
    if settings.vercel_env == "preview":
        raise HTTPException(403, "Manueller Sync ist in Preview-Deployments deaktiviert.")
    try:
        # No hourly bucket: an explicit refresh may run again within the same hour.
        # collect() still acquires the shared database lease used by Cron.
        result = collect()
    except Exception:
        raise HTTPException(503, "Synchronisierung fehlgeschlagen. Serverkonfiguration und Importstatus prüfen.") from None
    status = result["status"]
    if status == "already_running":
        raise HTTPException(409, "Ein Sync läuft bereits. Bitte nach dessen Abschluss erneut versuchen.")
    if status in ("failed", "partial"):
        raise HTTPException(503, "Synchronisierung fehlgeschlagen oder nur teilweise abgeschlossen. Details stehen beim letzten Import.")
    if status == "deferred":
        return {"status": status, "detail": "Zeitbudget erreicht. Fortschritt gespeichert; der nächste Sync setzt die Verarbeitung fort."}
    return {"status": status}


class BackfillInput(BaseModel):
    retry_failed: bool = False


@app.post("/api/backfill", dependencies=[Depends(authenticate)])
def backfill_run(data: BackfillInput | None = None):
    if settings.vercel_env == "preview":
        raise HTTPException(403, "Backfill ist in Preview-Deployments deaktiviert.")
    try:
        # Own lease: the hourly cron sync is never blocked by a running backfill.
        result = backfill_module.run(retry_failed=bool(data and data.retry_failed))
    except Exception:
        raise HTTPException(503, "Backfill fehlgeschlagen. Serverkonfiguration und Backfill-Status prüfen.") from None
    status = result["status"]
    if status == "already_running":
        raise HTTPException(409, "Ein Backfill läuft bereits. Bitte nach dessen Abschluss erneut versuchen.")
    if status == "failed":
        raise HTTPException(503, "Backfill fehlgeschlagen. Details stehen im Backfill-Status.")
    return jsonable_encoder(result)


@app.get("/api/backfill", dependencies=[Depends(authenticate)])
def backfill_status(s=Depends(db)):
    return jsonable_encoder({**backfill_module.summary(s), "progress": backfill_module.detail(s)})


@app.get("/api/learning", dependencies=[Depends(authenticate)])
def learning_overview(s=Depends(db)):
    return jsonable_encoder(learning_module.overview(s))


@app.get("/api/growth", dependencies=[Depends(authenticate)])
def growth_overview(s=Depends(db)):
    return jsonable_encoder(growth_module.overview(s))


@app.get("/api/discovery", dependencies=[Depends(authenticate)])
def discovery_overview(s=Depends(db)):
    return jsonable_encoder(discovery_module.overview(s))


@app.post("/api/discovery/run", dependencies=[Depends(authenticate)])
def discovery_run():
    if settings.vercel_env == "preview":
        raise HTTPException(403, "Discovery ist in Preview-Deployments deaktiviert.")
    from .youtube import YouTube
    from .budget import Budget
    try:
        # Read-only public lookups within the daily quota budget; own lease, idempotent per day.
        result = discovery_module.run(YouTube(), utcnow(), Budget(settings.sync_budget_seconds), force=True)
    except Exception:
        raise HTTPException(503, "Discovery fehlgeschlagen. Serverkonfiguration und Discovery-Status prüfen.") from None
    if result["status"] == "already_running":
        raise HTTPException(409, "Ein Discovery-Lauf läuft bereits.")
    if result["status"] == "failed":
        raise HTTPException(503, "Discovery fehlgeschlagen. Details im Discovery-Status.")
    return jsonable_encoder(result)


@app.post("/api/learning/rebuild", dependencies=[Depends(authenticate)])
def learning_rebuild(s=Depends(db)):
    if settings.vercel_env == "preview":
        raise HTTPException(403, "Lernlauf ist in Preview-Deployments deaktiviert.")
    from .budget import Budget
    try:
        # Idempotent upserts keyed by dataset signature; a concurrent sync cannot duplicate results.
        result = learning_module.refresh(s, utcnow(), Budget(settings.sync_budget_seconds), force=True)
    except Exception:
        raise HTTPException(503, "Lernlauf fehlgeschlagen oder Zeitbudget erreicht. Details im Lernstatus.") from None
    return jsonable_encoder(result)


@app.get("/api/dashboard", dependencies=[Depends(authenticate)])
def dashboard(s=Depends(db)):
    channels = list(s.scalars(select(Channel)))
    run = s.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
    evaluated = list(s.scalars(select(Forecast).where(Forecast.actual_views.is_not(None))))
    return {"channels": jsonable_encoder(channels), "videos": dashboard_rows(s),
            "sync": jsonable_encoder(run), "demo": any(c.id.startswith("DEMO") for c in channels),
            "backfill": jsonable_encoder(backfill_module.summary(s)),
            "learning_v4": jsonable_encoder(learning_module.overview(s)),
            "growth_v5": jsonable_encoder(growth_module.overview(s)),
            "discovery_v6": jsonable_encoder(discovery_module.overview(s)),
            "learning": {"evaluated_forecasts": len(evaluated),
                         "mae": sum(r.absolute_error for r in evaluated)/len(evaluated) if evaluated else None},
            "availability": {"returning_viewers": "Nicht Ã¼ber die verwendeten APIs verfÃ¼gbar",
                             "reach": "YouTube Reporting API; historische VerfÃ¼gbarkeit begrenzt",
                             "probabilities": "Horizontbezogen; erst ab 30 unabhÃ¤ngigen Modellfehlern"}}


@app.get("/api/videos/{video_id}", dependencies=[Depends(authenticate)])
def video_detail(video_id: str, s=Depends(db)):
    v = s.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video nicht gefunden")
    reports = list(s.scalars(select(Report).where(Report.video_id == video_id).order_by(Report.end.desc())))
    latest = {}
    for r in reports:
        latest.setdefault(r.kind, jsonable_encoder(r))
    revenue = latest.get("revenue")
    monetization = {"revenue": None, "rpm": None, "currency": "USD", "viewer_lifetime_value": None}
    if revenue and revenue["rows"]:
        amount = sum(r["estimatedRevenue"] for r in revenue["rows"])
        views = sum(r["views"] for r in revenue["rows"])
        monetization.update(revenue=amount, rpm=amount/views*1000 if views else None,
                            start=revenue["start"], end=revenue["end"])
    return {"video": jsonable_encoder(v),
            "snapshots": jsonable_encoder(list(s.scalars(select(Snapshot).where(Snapshot.video_id == video_id).order_by(Snapshot.observed_at.desc()).limit(1000)))[::-1]),
            "daily": jsonable_encoder(list(s.scalars(select(Daily).where(Daily.video_id == video_id).order_by(Daily.day.desc()).limit(365)))[::-1]),
            "reach": jsonable_encoder(list(s.scalars(select(Reach).where(Reach.video_id == video_id).order_by(Reach.day.desc()).limit(90)))[::-1]),
            "reports": latest, "monetization": monetization,
            "history": jsonable_encoder(backfill_module.video_history(s, video_id)),
            "strategy": jsonable_encoder(learning_module.overview(s)["videos"].get(video_id)),
            "growth": jsonable_encoder({**growth_module.overview(s)["scores"].get(video_id, {}),
                                        "actions": growth_module.overview(s)["actions"].get(video_id, [])}),
            "growth_history": jsonable_encoder(list(s.scalars(select(GrowthAssessment)
                .where(GrowthAssessment.video_id == video_id).order_by(GrowthAssessment.origin_at.desc()).limit(168))))}


@app.get("/api/memory", dependencies=[Depends(authenticate)])
def memory(s=Depends(db)):
    rows = list(s.scalars(select(Decision).order_by(Decision.id.desc())))
    return [{"decision": jsonable_encoder(r), "evidence": evidence(s, r.strategy_key),
             "prediction_outcomes": [{"forecast_id":p.id,"predicted_views":p.predicted_views,"actual_views":p.actual_views,"absolute_error":p.absolute_error}
                for p in s.scalars(select(Forecast).where(Forecast.id.in_((r.measurement or {}).get("forecast_ids",[]))))],
             "changes": jsonable_encoder(list(s.scalars(select(ExperimentChange).where(ExperimentChange.decision_id == r.id).order_by(ExperimentChange.recorded_at)))) ,
             "reviews": jsonable_encoder(list(s.scalars(select(MemoryReview).where(MemoryReview.decision_id == r.id))))} for r in rows]


@app.post("/api/memory", dependencies=[Depends(authenticate)], status_code=201)
def memory_create(data: DecisionInput, s=Depends(db)):
    try:
        row = create_decision(s, data)
        s.commit()
        return jsonable_encoder(row)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class Activation(BaseModel):
    video_id: str = Field(min_length=1, max_length=64)


@app.post("/api/memory/{decision_id}/activate", dependencies=[Depends(authenticate)])
def memory_activate(decision_id: int, data: Activation, s=Depends(db)):
    # Row lock serializes duplicate requests in PostgreSQL.
    row = s.scalar(select(Decision).where(Decision.id == decision_id).with_for_update())
    if not row:
        raise HTTPException(404, "Hypothese nicht gefunden")
    s.scalar(select(Video).where(Video.id == data.video_id).with_for_update())
    try:
        activate(s, row, data.video_id)
        s.commit()
        return jsonable_encoder(row)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class ReviewInput(BaseModel):
    suspected_cause: str = Field(min_length=3, max_length=2000)
    next_hypothesis: str = Field(min_length=8, max_length=2000)


@app.post("/api/memory/{decision_id}/reviews", dependencies=[Depends(authenticate)])
def memory_review(decision_id: int, data: ReviewInput, s=Depends(db)):
    row = s.get(Decision, decision_id)
    if not row:
        raise HTTPException(404, "Hypothese nicht gefunden")
    if row.status != "evaluated":
        raise HTTPException(422, "Ergebnis noch nicht verfÃ¼gbar")
    s.add(MemoryReview(decision_id=decision_id, **data.model_dump()))
    row.suspected_cause, row.next_hypothesis = data.suspected_cause, data.next_hypothesis
    s.commit()
    return {"status": "recorded"}


app.mount("/static", StaticFiles(directory=static), name="static")


class ChangeInput(BaseModel):
    applied_at: AwareDatetime
    dimension: Literal["content", "title", "thumbnail", "hook", "audience"]
    before_value: str = Field(min_length=1, max_length=2000)
    after_value: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=3, max_length=2000)


@app.post("/api/memory/{decision_id}/changes", dependencies=[Depends(authenticate)], status_code=201)
def record_change(decision_id: int, data: ChangeInput, s=Depends(db)):
    decision = s.get(Decision, decision_id)
    if decision is None:
        raise HTTPException(404, "Experiment not found")
    if decision.status == "draft" or data.applied_at > utcnow():
        raise HTTPException(422, "Register the experiment first and report an actual past change.")
    row = ExperimentChange(decision_id=decision_id, **data.model_dump())
    s.add(row)
    s.commit()
    return jsonable_encoder(row)


@app.get("/api/models/{run_id}", dependencies=[Depends(authenticate)])
def model_detail(run_id: int, s=Depends(db)):
    run=s.get(ModelRun,run_id)
    if run is None:
        raise HTTPException(404,"Model run not found")
    return jsonable_encoder(run)
