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
from .models import Channel, Video, Snapshot, Daily, Reach, Report, Forecast, SyncRun, Decision, MemoryReview, GrowthAssessment, ExperimentChange, utcnow
from .pipeline import dashboard_rows, collect
from .memory import DecisionInput, create_decision, activate, evidence

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


@app.get("/api/dashboard", dependencies=[Depends(authenticate)])
def dashboard(s=Depends(db)):
    channels = list(s.scalars(select(Channel)))
    run = s.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
    evaluated = list(s.scalars(select(Forecast).where(Forecast.actual_views.is_not(None))))
    return {"channels": jsonable_encoder(channels), "videos": dashboard_rows(s),
            "sync": jsonable_encoder(run), "demo": any(c.id.startswith("DEMO") for c in channels),
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
            "growth_history": jsonable_encoder(list(s.scalars(select(GrowthAssessment)
                .where(GrowthAssessment.video_id == video_id).order_by(GrowthAssessment.origin_at.desc()).limit(168))))}


@app.get("/api/memory", dependencies=[Depends(authenticate)])
def memory(s=Depends(db)):
    rows = list(s.scalars(select(Decision).order_by(Decision.id.desc())))
    return [{"decision": jsonable_encoder(r), "evidence": evidence(s, r.strategy_key),
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
