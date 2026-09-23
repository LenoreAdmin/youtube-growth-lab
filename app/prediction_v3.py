"""Auditable organic-only learning, chronological validation and honest cold start."""
from datetime import datetime, timedelta
from math import log1p
import numpy as np
import sklearn
from sqlalchemy import select
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from .models import Forecast, ModelRun, PredictionAudit, Decision
from .metrics import aware
from .prediction import mature
from .organic import eligibility
from .decisions import recommend

VERSION = "organic-prediction-v3"
RECHECK_HOURS = 24      # Provenienz wird taeglich neu geprueft, nicht stuendlich.
RECHECK_LIMIT = 150     # Obergrenze je Lauf: bounded Arbeit statt linear wachsender Kosten (24 Laeufe/Tag reichen fuer taegliche Abdeckung).
FEATURES = ["velocity","acceleration","ctr","retention","watchtime_efficiency",
            "watch_minutes","subscriber_conversion","traffic_search","traffic_suggested","traffic_external","age_days"]
REGIMES = ["baseline","rising","breakout","cooling"]


def vector(f):
    return [float(f.get(k) or 0) for k in FEATURES]+[float(f.get(k) is None) for k in FEATURES]+[float(f.get("growth_assessment",{}).get("regime")==r) for r in REGIMES]


def due_for_recheck(audit, now):
    """Noch nie geprueft oder letzter Check aelter als RECHECK_HOURS."""
    checked=(audit.feedback or {}).get("checked_at")
    if not checked:
        return True
    try:
        return (aware(now)-datetime.fromisoformat(checked)).total_seconds() >= RECHECK_HOURS*3600
    except ValueError:
        return True


def feedback(session, now, budget=None, limit=RECHECK_LIMIT):
    """Jede Provenienzpruefung kostet mehrere Datenbank-Roundtrips; deshalb bounded und hoechstens taeglich.

    Gemessen: ohne Begrenzung verursachte dieser Schritt allein ~9.400 Abfragen je Sync und wuchs
    mit jeder gespeicherten Prognose weiter. Aelteste Pruefung zuerst, damit jede Zeile drankommt.
    """
    mature(session, now)
    session.flush()
    rows=session.execute(select(Forecast,PredictionAudit).join(PredictionAudit).where(Forecast.actual_views.is_not(None))
        .order_by(PredictionAudit.created_at)).all()
    pending=[(f,a) for f,a in rows if due_for_recheck(a,now)][:limit]
    for forecast,audit in pending:
        if budget:
            budget.check()
        quality=eligibility(session,forecast,now)
        audit.feedback={**quality,"checked_at":aware(now).isoformat(),
            "actual_views":forecast.actual_views,"actual_at":aware(forecast.actual_at).isoformat(),
            "signed_error":forecast.actual_views-forecast.predicted_views,
            "absolute_error":forecast.absolute_error,
            "interval_hit":forecast.lower_views<=forecast.actual_views<=forecast.upper_views}


def training_history(session, cutoff, horizon, content_type, budget=None):
    if content_type in (None,"UNKNOWN"):
        return []
    rows=session.execute(select(Forecast,PredictionAudit).join(PredictionAudit).where(
        Forecast.horizon_hours==horizon,Forecast.actual_views.is_not(None),Forecast.actual_at<cutoff,
        PredictionAudit.created_at<cutoff).order_by(Forecast.origin_at.desc(),Forecast.id.desc()).limit(1000)).all()[::-1]
    selected=[]; last={}
    for row,audit in rows:
        if budget:
            budget.check()
        if row.features.get("content_type")!=content_type or row.features.get("prediction_version")!=VERSION:
            continue
        # Abgeschlossene Pruefung mit negativem Ergebnis: nicht erneut mit mehreren Abfragen pruefen.
        # Nur ein Eintrag mit checked_at ist ein Pruefergebnis; feedback() haelt es taeglich aktuell,
        # damit spaeter geeignete Zeilen wieder aufgenommen werden. Ungepruefte Zeilen werden voll geprueft.
        verdict=audit.feedback or {}
        if verdict.get("checked_at") and verdict.get("status")!="eligible_organic":
            continue
        # Recheck provenance as of this origin. Later corrected paid reports invalidate old eligibility.
        if eligibility(session,row,cutoff)["status"]!="eligible_organic":
            continue
        if row.video_id in last and aware(row.origin_at)<last[row.video_id]:
            continue
        if row.actual_views<row.origin_views:
            continue
        selected.append(row); last[row.video_id]=aware(row.target_at)
    return selected


def target_probability(samples, threshold, origin, video_count):
    if origin>=threshold:
        return {"status":"already_reached_total_counter","probability":None}
    if len(samples)<30 or video_count<30:
        return {"status":"insufficient_data","probability":None}
    hits=int(np.sum(samples>=threshold)); n=len(samples)
    if min(hits,n-hits)<5:
        return {"status":"insufficient_data","probability":None}
    p=hits/n; z=1.96
    center=(p+z*z/(2*n))/(1+z*z/n)
    radius=z*((p*(1-p)/n+z*z/(4*n*n))**.5)/(1+z*z/n)
    return {"status":"empirical_conditional_estimate","probability":(hits+1)/(n+2),
            "interval_95":[max(0,center-radius),min(1,center+radius)],"n_videos":video_count}


def predict(session,video_id,origin,f,horizon,budget=None):
    cutoff=aware(origin.observed_at)
    f={**f,"prediction_version":VERSION}
    history=training_history(session,cutoff,horizon,f.get("content_type"),budget)
    n=len(history); videos=len({r.video_id for r in history})
    baseline=max(0,f["velocity"])*horizon
    factor=1.0; mode="baseline"; gain=baseline
    if n>=30 and videos>=5:
        factor=float(np.clip(np.median([(r.actual_views-r.origin_views+1)/(max(0,r.features["velocity"])*horizon+1) for r in history]),.1,5))
        gain=max(0,(baseline+1)*factor-1); mode="adaptive_baseline"
    parameters={"version":VERSION,"features":FEATURES,"missing_flags":FEATURES,"regime_features":REGIMES,
        "cutoff":cutoff.isoformat(),"libraries":{"numpy":np.__version__,"scikit_learn":sklearn.__version__},"training_forecast_ids":[r.id for r in history],"factor":factor,
        "baseline_gain":baseline,"policy":{"min_adaptive_rows":30,"min_adaptive_videos":5,"min_ridge_rows":80}}
    evaluation={"status":"insufficient_data","n":n,"n_videos":videos,"accepted":False}
    if n>=80 and videos>=10:
        validation=history[int(n*.75):]
        validation_ids={r.video_id for r in validation}
        train=[r for r in history[:int(n*.75)] if aware(r.actual_at)<aware(validation[0].origin_at) and r.video_id not in validation_ids]
        if len(train)>=40 and len(validation)>=20 and len({r.video_id for r in train})>=5:
            def fit(rows):
                model=make_pipeline(StandardScaler(),Ridge(alpha=10))
                model.fit([vector(r.features) for r in rows],[log1p(r.actual_views-r.origin_views) for r in rows])
                return model
            candidate=fit(train)
            actual=np.array([r.actual_views-r.origin_views for r in validation])
            estimates=np.maximum(0,np.expm1(np.clip(candidate.predict([vector(r.features) for r in validation]),0,25)))
            raw=np.array([max(0,r.features["velocity"])*horizon for r in validation])
            train_factor=float(np.clip(np.median([(r.actual_views-r.origin_views+1)/(max(0,r.features["velocity"])*horizon+1) for r in train]),.1,5))
            mae=float(np.mean(np.abs(estimates-actual)))
            base_mae=min(float(np.mean(np.abs(raw-actual))),float(np.mean(np.abs(np.maximum(0,(raw+1)*train_factor-1)-actual))))
            parameters["validation_candidate"]={"scaler_mean":candidate[0].mean_.tolist(),
                "scaler_scale":candidate[0].scale_.tolist(),"coefficients":candidate[1].coef_.tolist(),
                "intercept":float(candidate[1].intercept_),"alpha":10}
            accepted=mae<base_mae*.95
            evaluation.update(status="chronological_video_holdout",mae=mae,baseline_mae=base_mae,accepted=accepted,
                training_ids=[r.id for r in train],validation_ids=[r.id for r in validation])
            if accepted:
                champion=fit(history)
                gain=float(max(0,np.expm1(np.clip(champion.predict([vector(f)])[0],0,25))))
                parameters.update(scaler_mean=champion[0].mean_.tolist(),scaler_scale=champion[0].scale_.tolist(),
                    coefficients=champion[1].coef_.tolist(),intercept=float(champion[1].intercept_))
                mode="ridge"
    version=VERSION+"/"+mode
    # One saved out-of-sample error per video, same model family and regime only.
    calibration={}
    for row in history:
        if row.model_version==version and row.features.get("growth_assessment",{}).get("regime")==f.get("growth_assessment",{}).get("regime"):
            calibration[row.video_id]=row
    errors=[log1p(r.actual_views-r.origin_views)-log1p(max(0,r.predicted_views-r.origin_views)) for r in calibration.values()]
    samples=np.maximum(0,np.expm1(np.clip(log1p(gain)+np.array(errors),0,25)))+origin.views
    missing=[k for k in FEATURES if f.get(k) is None]
    calibrated=len(errors)>=30 and len(missing)<=2 and f.get("growth_assessment",{}).get("regime") in REGIMES
    lower,upper=[float(v) for v in np.quantile(samples,[.1,.9])] if calibrated else [origin.views+gain*.25,origin.views+gain*4]
    targets={str(t):target_probability(samples if calibrated else np.array([]),t,origin.views,len(calibration)) for t in (100000,1000000)}
    confidence="empirical_limited" if calibrated else "insufficient_data"
    linked=list(session.scalars(select(Decision.id).where(Decision.video_id==video_id,Decision.created_at<=cutoff,
        Decision.status.in_(["registered","evaluated"]))))
    parameters.update(mode=mode,calibration_forecast_ids=[r.id for r in calibration.values()],
                      log_residuals=errors,predicted_gain=gain)
    run=ModelRun(created_at=cutoff,horizon_hours=horizon,training_rows=n,parameters=parameters,metrics=evaluation)
    session.add(run); session.flush()
    forecast=Forecast(video_id=video_id,origin_at=cutoff,target_at=cutoff+timedelta(hours=horizon),horizon_hours=horizon,
        origin_views=origin.views,predicted_views=origin.views+gain,lower_views=lower,upper_views=upper,
        p100k=targets["100000"]["probability"],p1m=targets["1000000"]["probability"],features=f,model_version=version,calibration_n=len(errors))
    session.add(forecast);session.flush()
    session.add(PredictionAudit(forecast_id=forecast.id,model_run_id=run.id,created_at=cutoff,
        metadata_json={"version":VERSION,"model_run_id":run.id,"confidence":confidence,"targets":targets,"interval_kind":"empirical_80" if calibrated else "uncalibrated_scenario",
            "scope":"total_counter_conditional_on_organic_future_growth","origin_organic_status":"unverified_total_counter",
            "data_quality":f.get("metric_status",{}),"missing_features":[k for k in FEATURES if f.get(k) is None],
            "regime":f.get("growth_assessment",{}).get("regime","unknown"),"linked_experiment_ids":linked},
        feedback={"status":"pending_outcome"},recommendations=recommend(f,confidence,linked,dict(zip(FEATURES,parameters.get("coefficients",[]))))))
    return forecast
