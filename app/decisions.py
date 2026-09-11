"""Deterministic experiment proposals, not automatic channel actions or causal claims."""
VERSION = "decision-rules-v3"


def recommend(f, confidence, linked_ids, weights=None):
    quality = f.get("metric_status", {})
    regime = f.get("growth_assessment", {}).get("regime", "unknown")
    ideas = [
        ("positioning", "Eine klar abgegrenzte musikalische Positionierung kÃ¶nnte wiederkehrendes Interesse erhÃ¶hen.", "Eine Themenreihe gegen ein vergleichbares Video vorregistrieren.", "subscriber_conversion"),
        ("audience", "Eine explizite Nutzungssituation kÃ¶nnte die passende Zielgruppe besser erreichen.", "Eine Zielgruppenansprache variieren, Format und VerÃ¶ffentlichungskontext konstant halten.", "watchtime_efficiency"),
        ("content_format", "Ein verwandtes Format kÃ¶nnte das aktuelle Zuschauerinteresse bedienen.", "Ein Folgevideo mit vergleichbarer LÃ¤nge testen; Themenunterschied als StÃ¶rfaktor erfassen.", "velocity"),
        ("title_thumbnail", "Eine klarere Erwartung im Titel oder Thumbnail kÃ¶nnte qualifizierte Klicks fÃ¶rdern.", "Nur Titel ODER Thumbnail Ã¤ndern, Zeitpunkt protokollieren und CTR plus Watchtime vergleichen.", "ctr"),
        ("hook", "Ein frÃ¼her klarer musikalischer Einstieg kÃ¶nnte Zuschauer lÃ¤nger halten.", "Einstieg im nÃ¤chsten vergleichbaren Video variieren; Retention und Watchtime gemeinsam prÃ¼fen.", "retention"),
        ("release", "Eine konsistente VerÃ¶ffentlichungsfolge kÃ¶nnte Folgeaufrufe erleichtern.", "Zeitfenster Ã¼ber mehrere vergleichbare Uploads testen; Thema, Saison und Wochentag protokollieren.", "velocity"),
    ]
    weights=weights or {}
    strongest=max((abs(v) for v in weights.values()),default=0)
    rows=[]
    for dimension,hypothesis,test,metric in ideas:
        signal=f.get(metric)
        priority=2
        if dimension=="content_format" and regime in ("rising","breakout"):
            priority=1
        if dimension=="hook" and f.get("watchtime_efficiency") is not None and f["watchtime_efficiency"] < .35:
            priority=1
        association=weights.get(metric)
        if association is not None and strongest and abs(association)>=strongest*.75:
            priority=1
        rows.append(dict(model_association=association,dimension=dimension,priority=priority,hypothesis=hypothesis,test=test,
            objective=metric,signal=signal,status="testable_hypothesis",confidence=confidence,
            evidence="observational" if signal is not None else "insufficient_data",
            linked_experiment_ids=linked_ids,version=VERSION,
            guardrail="Vorregistrieren; mÃ¶glichst eine Dimension Ã¤ndern; keine kausale Zusicherung. Nicht automatisch ausfÃ¼hren."))
    if confidence=="insufficient_data" or any(v!="available" for v in quality.values()):
        rows.insert(0,dict(dimension="measurement",priority=0,hypothesis="VollstÃ¤ndige Daten kÃ¶nnten belastbarere Entscheidungen erlauben.",
            test="Traffic-Abdeckung, Snapshot-LÃ¼cken und verzÃ¶gerte Analytics prÃ¼fen; mehrere unabhÃ¤ngige Uploads erfassen.",
            objective="data_quality",status="testable_hypothesis",confidence="insufficient_data",version=VERSION))
    return sorted(rows,key=lambda row:row["priority"])
