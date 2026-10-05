"""Content Intelligence: aus gemessenen Eigenschaften des eigenen Materials produzierbare Kandidaten.

Dieses Modul enthaelt **keine** Medienabhaengigkeit. Es liest ausschliesslich, was der lokale Lauf
(`app.media`, `python -m app.cli content`) nach Neon geschrieben hat, und kombiniert es mit der
gemessenen Retentionskurve und der belegten Audience-/Placement-Chance. Damit laeuft es unveraendert
in einer Vercel-Funktion.

Drei Regeln, die hier durchgaengig gelten:

1. **Keine erfundenen Labels.** Gespeichert und bewertet werden Messwerte – Energie, Wiederholung,
   Gesangsanteil, Abschnittsgrenze, Bildschnitt. Ob ein Abschnitt ein „Refrain“ ist, wird nicht
   behauptet; die Entscheidung funktioniert ohne diese Benennung.
2. **Kein einzelner Gewinner.** Ein Video liefert mehrere messbar unterschiedliche Kandidaten mit
   exakten Zeiten. Welcher zuerst produziert wird, ist eine Reihenfolge aus belegten Faktoren – keine
   Vorhersage.
3. **Lernen aus echter Distribution.** Erst die eigenen Impressions, Views, Trafficquellen und die
   Retention des veroeffentlichten Shorts entscheiden, welche Eigenschaften kuenftig vorne stehen.
   Solange zu wenige veroeffentlicht sind, bleiben die Gewichte neutral und sagen das auch.
"""
import logging
from datetime import date, timedelta
from sqlalchemy import select

log = logging.getLogger("content")

SHORT_MAX = 60.0            # YouTube-Shorts-Grenze; laengeres Material ist kein Short.
SHORT_MIN = 12.0            # Kuerzer traegt keinen eigenen Inhalt.
DISTINCT_SECONDS = 8.0      # Zwei Kandidaten muessen sich zeitlich deutlich unterscheiden.
DISTINCT_OVERLAP = 0.5      # ... und duerfen sich hoechstens zur Haelfte ueberlappen.
MAX_CANDIDATES = 4          # Mehr als vier Vorschlaege je Video sind keine Auswahl, sondern eine Liste.
HOOK_MIN_CONFIDENCE = 0.75  # Darunter ist eine maschinell erkannte Zeile kein Zitat.
HOOK_WINDOW = 4.0           # Eine Zeile taugt als Hook, wenn sie am Anfang des Kandidaten einsetzt.
MIN_PUBLISHED_FOR_PRIORS = 3  # Unter drei veroeffentlichten Shorts wird nichts „gelernt“.
PRIOR_WINDOW_DAYS = 14      # Fenster, in dem die Distribution eines neuen Shorts gemessen wird.

# Die Eigenschaften, die gemessen werden und ueber die gelernt werden kann. Bewusst ohne Semantik.
MEASURED = ("energy_rel", "repetition_strength", "vocal_presence", "novelty", "visual_cut_density",
            "retention_rel")

LABELS = {"energy_rel": "Energie gegenueber dem eigenen Mittel",
          "repetition_strength": "Wiederholungsstaerke im Stueck",
          "vocal_presence": "Gesangsanteil",
          "novelty": "Schaerfe der Abschnittsgrenze",
          "visual_cut_density": "Bildschnitte je Sekunde",
          "retention_rel": "gemessene Retention gegenueber dem Kurvenmittel"}


def retention_profile(session, video):
    """Die gemessene Retentionskurve als abfragbares Profil – oder None, wenn es keine gibt."""
    from .models import VideoRetention
    if video is None:
        return None
    stored = session.get(VideoRetention, video.id)
    rows = sorted((r for r in (stored.rows if stored else []) if r.get("ratio") is not None),
                  key=lambda r: r.get("at") or 0)
    if not rows or not video.duration_seconds:
        return None
    mean = sum(r["ratio"] for r in rows)/len(rows)
    return {"rows": rows, "mean": mean, "points": len(rows), "duration": float(video.duration_seconds)}


def retention_window(profile, start, end):
    """Mittlere Retention im Fenster [start, end] in Sekunden, relativ zum Mittel der eigenen Kurve."""
    if not profile or profile["duration"] <= 0 or profile["mean"] <= 0:
        return None
    lo, hi = start/profile["duration"], end/profile["duration"]
    inside = [r["ratio"] for r in profile["rows"] if lo <= (r.get("at") or 0) <= hi]
    if not inside:
        return None
    return {"ratio": round(sum(inside)/len(inside), 4),
            "relative": round((sum(inside)/len(inside))/profile["mean"], 3),
            "points": len(inside)}


def _window_end(section, sections, duration):
    """Ende des Kandidaten: die naechste gemessene Abschnittsgrenze im erlaubten Bereich, sonst die Grenze.

    So endet ein Short an einer Stelle, die im Material tatsaechlich eine Grenze ist – und nicht nach
    einer runden Sekundenzahl.
    """
    start = section.start_seconds
    latest = min(start+SHORT_MAX, duration)
    boundaries = sorted(s.start_seconds for s in sections
                        if start+SHORT_MIN <= s.start_seconds <= latest)
    if section.end_seconds and start+SHORT_MIN <= section.end_seconds <= latest:
        boundaries.append(section.end_seconds)
    if boundaries:
        return max(boundaries), "boundary"
    # Ohne Grenze endet das Fenster entweder am Material oder an der Shorts-Grenze – nicht beliebig.
    return latest, ("material" if latest >= duration-0.01 else "limit")


def _hook(lines, start):
    """Die tatsaechlich an dieser Stelle gesungene Zeile, woertlich – oder None.

    Ein Zitat aus dem eigenen Material ist keine Erfindung. Eine maschinell erkannte Zeile unterhalb
    der Sicherheitsschwelle ist dagegen eine Vermutung und wird nie als Hook ausgegeben.
    """
    for line in lines:
        if not (start-0.5 <= line.start_seconds <= start+HOOK_WINDOW):
            continue
        if not (line.text or "").strip():
            continue
        if line.source == "aligned" or (line.confidence or 0) >= HOOK_MIN_CONFIDENCE:
            return {"text": line.text.strip(), "source": line.source,
                    "confidence": line.confidence, "at": line.start_seconds}
    return None


def _vocal(section, lines):
    """Gesangsanteil: gemessen, wenn Text mit Zeitmarken vorliegt, sonst der Messwert aus dem Audio."""
    span = max(0.001, (section.end_seconds or 0)-(section.start_seconds or 0))
    covered = 0.0
    for line in lines:
        lo = max(line.start_seconds, section.start_seconds)
        hi = min(line.end_seconds, section.end_seconds)
        if hi > lo:
            covered += hi-lo
    if covered > 0:
        return round(min(1.0, covered/span), 3)
    return section.vocal_presence


def asset_for(session, video):
    """Die analysierte eigene Datei zu diesem Video – oder None, wenn keine zugeordnet ist."""
    from .models import MediaAsset
    return session.scalar(select(MediaAsset).where(MediaAsset.video_id == video.id,
                                                   MediaAsset.status == "analysed")
                          .order_by(MediaAsset.analysed_at.desc()))


def distinct_enough(chosen, candidate):
    """Unterscheiden sich zwei Kandidaten tatsaechlich – zeitlich und in mindestens einem Messwert?

    Zwei Fenster, die praktisch denselben Abschnitt zeigen, sind keine Auswahl. Gefordert ist Abstand
    im Startpunkt, begrenzte Ueberlappung und ein erkennbarer Unterschied in den Eigenschaften.
    """
    for other in chosen:
        gap = abs(candidate["start_seconds"]-other["start_seconds"])
        overlap = (min(candidate["end_seconds"], other["end_seconds"])
                   - max(candidate["start_seconds"], other["start_seconds"]))
        shorter = min(candidate["end_seconds"]-candidate["start_seconds"],
                      other["end_seconds"]-other["start_seconds"])
        share = max(0.0, overlap)/shorter if shorter > 0 else 1.0
        if gap < DISTINCT_SECONDS or share > DISTINCT_OVERLAP:
            return False
        differs = any(abs((candidate["properties"].get(k) or 0)-(other["properties"].get(k) or 0)) >= 0.1
                      for k in MEASURED)
        if not differs:
            return False
    return True


def priors(session, today=None):
    """Was die tatsaechliche Distribution bereits veroeffentlichter Shorts ueber Eigenschaften sagt.

    Grundlage sind ausschliesslich eigene gemessene Daten des neuen Assets: Views und Watchtime im
    Fenster nach der Veroeffentlichung sowie der Anteil aus Shorts-Feed und Browse. Je Eigenschaft
    wird verglichen, ob die Haelfte mit dem hoeheren Messwert mehr Auslieferung erhielt als die
    andere. Unterhalb von MIN_PUBLISHED_FOR_PRIORS bleibt alles neutral – ein oder zwei Shorts sind
    kein gelerntes Muster, und das wird ausgesprochen statt verdeckt.
    """
    from .models import ContentCandidate, Daily, TrafficDaily
    today = today or date.today()
    rows = list(session.scalars(select(ContentCandidate)
                                .where(ContentCandidate.published_video_id.is_not(None),
                                       ContentCandidate.published_day.is_not(None))))
    observed = []
    for row in rows:
        end = row.published_day+timedelta(days=PRIOR_WINDOW_DAYS)
        daily = list(session.scalars(select(Daily).where(Daily.video_id == row.published_video_id,
                                                        Daily.day >= row.published_day, Daily.day <= end)))
        if not daily:
            continue
        traffic = list(session.scalars(select(TrafficDaily)
                                       .where(TrafficDaily.video_id == row.published_video_id,
                                              TrafficDaily.day >= row.published_day,
                                              TrafficDaily.day <= end, TrafficDaily.paid.is_(False))))
        views = sum(d.views for d in daily)
        algorithmic = sum(t.views for t in traffic if t.source in ("SHORTS", "BROWSE", "SUGGESTED"))
        observed.append({"candidate_id": row.id, "properties": row.properties or {},
                         "views": views, "algorithmic_views": algorithmic,
                         "days": len(daily),
                         "retention": round(sum(d.average_percentage for d in daily)/len(daily), 2)})
    if len(observed) < MIN_PUBLISHED_FOR_PRIORS:
        return {"weights": {}, "basis": len(observed), "learning": False,
                "note": (f"{len(observed)} veroeffentlichte Shorts mit eigenen Messdaten – unter "
                         f"{MIN_PUBLISHED_FOR_PRIORS} wird aus der Distribution nichts abgeleitet. "
                         "Die Reihenfolge kommt allein aus gemessenen Materialeigenschaften."),
                "observed": observed}
    weights = {}
    for key in MEASURED:
        values = [(o["properties"].get(key), o) for o in observed if o["properties"].get(key) is not None]
        if len(values) < MIN_PUBLISHED_FOR_PRIORS:
            continue
        values.sort(key=lambda pair: pair[0])
        half = len(values)//2
        low = [pair[1]["algorithmic_views"] for pair in values[:half]]
        high = [pair[1]["algorithmic_views"] for pair in values[len(values)-half:]]
        if not low or not high:
            continue
        low_mean, high_mean = sum(low)/len(low), sum(high)/len(high)
        total = low_mean+high_mean
        if total <= 0:
            continue
        # Ein Wert zwischen -1 und 1: positiv heisst, der hoehere Messwert erhielt mehr Auslieferung.
        weights[key] = round((high_mean-low_mean)/total, 3)
    return {"weights": weights, "basis": len(observed), "learning": bool(weights),
            "note": (f"Gelernt aus {len(observed)} veroeffentlichten Shorts: Vergleich der eigenen "
                     f"algorithmischen Auslieferung (Shorts-Feed, Browse, Suggested) in {PRIOR_WINDOW_DAYS} "
                     "Tagen nach Veroeffentlichung." if weights else
                     "Veroeffentlichte Shorts vorhanden, aber ohne auswertbare Auslieferungsunterschiede."),
            "observed": observed}


def _order(candidate, external, learned):
    """Die gemeinsame Ordnung aus Inhalt, Retention und belegter Chance – ohne Prognosezahl.

    Jeder Beitrag kommt aus einem Messwert. Gewichte aus tatsaechlicher Distribution wirken nur, wenn
    sie gelernt wurden; sonst zaehlen die Materialeigenschaften gleichwertig.
    """
    props = candidate["properties"]
    content = 0.0
    for key in MEASURED:
        value = props.get(key)
        if value is None:
            continue
        weight = learned.get(key)
        content += value*(1.0+weight) if weight is not None else value
    chance = float((external or {}).get("score") or 0)/100.0
    hook = 0.25 if candidate.get("hook") else 0.0
    return round(content+chance+hook, 4)


def candidates(session, video, external=None, learned=None, today=None):
    """Kandidaten zu einem veroeffentlichten Video – falls eine eigene Datei dazu analysiert ist.

    Ohne analysierte Datei gibt es hier nichts: dann bleibt die Engine beim retentionsbasierten
    Kandidatenmaterial. Es wird nichts erfunden und kein Abschnitt benannt.
    """
    asset = asset_for(session, video)
    if asset is None:
        return []
    return candidates_for(session, asset, video, external, learned)


def candidates_for(session, asset, video=None, external=None, learned=None):
    """Kandidaten direkt aus einer analysierten Datei – exakte Zeiten, belegte Eigenschaften.

    Ein Video ist nicht erforderlich: neues Material existiert, bevor es ein veroeffentlichtes Video
    gibt. Liegt eines vor, kommt die gemessene Retentionskurve als weiterer Faktor hinzu; fehlt sie,
    steht das in der Evidenz und wird nicht ersetzt.
    """
    from .models import ContentCandidate, ContentSection, ContentLine
    # Bereits veroeffentlichte Fenster: verbraucht, und ihr Bereich ist fuer neue Kandidaten belegt.
    used = {(row.start_seconds, row.end_seconds): row.published_video_id
            for row in session.scalars(select(ContentCandidate)
                                       .where(ContentCandidate.asset_id == asset.id,
                                              ContentCandidate.published_video_id.is_not(None)))}
    sections = list(session.scalars(select(ContentSection).where(ContentSection.asset_id == asset.id)
                                    .order_by(ContentSection.idx)))
    if not sections:
        return []
    lines = list(session.scalars(select(ContentLine).where(ContentLine.asset_id == asset.id)
                                 .order_by(ContentLine.idx)))
    duration = float(asset.duration_seconds or (video.duration_seconds if video else 0) or 0)
    if duration <= 0:
        return []
    profile = retention_profile(session, video) if video is not None else None
    learned = (learned or {}).get("weights", {}) if isinstance(learned, dict) and "weights" in (learned or {}) else (learned or {})
    built = []
    for section in sections:
        start = float(section.start_seconds or 0)
        end, end_reason = _window_end(section, sections, duration)
        if end-start < SHORT_MIN:
            continue
        retention = retention_window(profile, start, end)
        properties = {"energy_rel": section.energy_rel, "repetition_strength": section.repetition_strength,
                      "vocal_presence": _vocal(section, lines), "novelty": section.novelty,
                      "visual_cut_density": section.visual_cut_density,
                      "retention_rel": (retention or {}).get("relative")}
        hook = _hook(lines, start)
        evidence = []
        if section.energy_rel is not None:
            evidence.append(f"Energie {section.energy_rel:.2f}x gegenueber dem Mittel des Stuecks")
        if section.repetition_strength is not None:
            evidence.append(f"Wiederholungsstaerke {section.repetition_strength:.2f}"
                            + (f" (Gruppe {section.repetition_group})" if section.repetition_group is not None else ""))
        if properties["vocal_presence"] is not None:
            evidence.append(f"Gesangsanteil {properties['vocal_presence']:.2f}")
        if section.boundary_cut_distance is not None:
            evidence.append(f"Start liegt {section.boundary_cut_distance:.2f} s von einem Bildschnitt entfernt")
        if section.visual_cuts is not None:
            evidence.append(f"{section.visual_cuts} Bildschnitte im Fenster")
        if retention:
            evidence.append(f"Retention {retention['relative']:.2f}x des Kurvenmittels ueber "
                            f"{retention['points']} gemessene Punkte")
        else:
            evidence.append("keine Retentionspunkte in diesem Fenster – Auswahl allein aus Materialeigenschaften")
        evidence.append({"boundary": "Ende auf einer gemessenen Abschnittsgrenze",
                         "material": "Ende am Ende des Materials",
                         "limit": f"Ende bei {SHORT_MAX:.0f} s Shorts-Grenze"}[end_reason])
        candidate = {"asset_id": asset.id, "video_id": (video.id if video else None),
                     "section": section.idx,
                     "start_seconds": round(start, 2), "end_seconds": round(end, 2),
                     "duration_seconds": round(end-start, 2), "properties": properties,
                     "evidence": evidence, "hook": (hook or {}).get("text"),
                     "hook_source": (hook or {}).get("source"),
                     "hook_confidence": (hook or {}).get("confidence"),
                     "retention": retention}
        candidate["published_video_id"] = used.get((candidate["start_seconds"], candidate["end_seconds"]))
        candidate["score"] = _order(candidate, external, learned)
        built.append(candidate)
    built.sort(key=lambda c: -c["score"])
    # Ein veroeffentlichtes Fenster bleibt in der Liste – seine Messung laeuft – und besetzt dabei
    # seinen Bereich, damit aus demselben Material kein zweiter, praktisch gleicher Short entsteht.
    chosen = [c for c in built if c.get("published_video_id")]
    for candidate in built:
        if candidate.get("published_video_id"):
            continue
        if distinct_enough(chosen, candidate):
            chosen.append(candidate)
        if len(chosen) == MAX_CANDIDATES:
            break
    for rank, candidate in enumerate(chosen, start=1):
        candidate["rank"] = rank
    return chosen


def store(session, video, chosen, today):
    """Kandidaten festhalten, damit die spaetere Distribution genau diesem Fenster zugeordnet werden kann.

    Ohne gespeicherten Kandidaten gibt es kein Lernen: der veroeffentlichte Short waere dann nur ein
    weiteres Video ohne Bezug zu Hook und Eigenschaften, aus denen er entstanden ist.
    """
    from .models import ContentCandidate
    kept = []
    for candidate in chosen:
        row = session.scalar(select(ContentCandidate)
                             .where(ContentCandidate.asset_id == candidate["asset_id"],
                                    ContentCandidate.start_seconds == candidate["start_seconds"],
                                    ContentCandidate.end_seconds == candidate["end_seconds"]))
        if row is None:
            row = ContentCandidate(asset_id=candidate["asset_id"], video_id=(video.id if video else None),
                                   start_seconds=candidate["start_seconds"],
                                   end_seconds=candidate["end_seconds"], created_day=today)
            session.add(row)
        row.properties = candidate["properties"]
        row.evidence = candidate["evidence"]
        row.hook, row.hook_source = candidate.get("hook"), candidate.get("hook_source")
        session.flush()
        kept.append({**candidate, "candidate_id": row.id, "render_path": row.render_path,
                     "published_video_id": row.published_video_id})
    return kept


def attach_published(session, candidate_id, published_video_id, published_day):
    """Den veroeffentlichten Short mit dem Kandidaten verbinden, aus dem er entstanden ist.

    Danach fliessen seine eigenen Impressions, Views, Trafficquellen und Retention ueber den normalen
    Sync ein und wirken ueber `priors()` auf die kuenftige Kandidatenauswahl.
    """
    from .models import ContentCandidate
    row = session.get(ContentCandidate, candidate_id)
    if row is None:
        return None
    row.published_video_id, row.published_day, row.status = published_video_id, published_day, "published"
    session.flush()
    return row


def material(session, video, external=None, today=None):
    """Das vollstaendige Materialbild fuer produce_for_opportunity: Kandidaten plus Lernstand."""
    learned = priors(session, today)
    chosen = candidates(session, video, external, learned, today)
    if not chosen:
        return None
    return {"candidates": chosen, "learning": learned, "asset_id": chosen[0]["asset_id"],
            "primary": chosen[0], "alternatives": chosen[1:]}


def vocabulary(session, video):
    """Der eigene belegte Wortlaut dieses Videos als Wortmenge – fuer das Packaging.

    Nur ausgerichtete oder ausreichend sichere Zeilen zaehlen. Eine unsichere maschinelle Erkennung
    darf keinen Titel und keine Beschreibung praegen; dann bleibt die Menge eben leer.
    """
    from .models import ContentLine
    from .audience import _tokens, HELPER_WORDS
    try:
        asset = asset_for(session, video)
        rows = (list(session.scalars(select(ContentLine).where(ContentLine.asset_id == asset.id)))
                if asset is not None else [])
    except Exception:
        # Inhaltswissen ist eine Ergaenzung, kein Betriebsrisiko: ohne Schicht bleibt die Menge leer.
        return set()
    words = set()
    for row in rows:
        if row.source != "aligned" and (row.confidence or 0) < HOOK_MIN_CONFIDENCE:
            continue
        words |= {t for t in _tokens(row.text or "") if t not in HELPER_WORDS}
    return words


# ---------------------------------------------------------------- Uploadfertige Maßnahme
TITLE_MAX = 100          # YouTube-Grenze fuer Titel.
EXCERPT = "Short"        # Sachlicher Zusatz, der den Ausschnitt vom ganzen Video unterscheidet.


def selected(candidates):
    """Genau ein Kandidat: der vorderste, der als fertig geschnittene Datei vorliegt.

    Die Reihenfolge entsteht bereits aus Messung, Retention, Chance und Gelerntem. Hier kommen zwei
    Bedingungen hinzu: ohne geschnittene Datei gibt es nichts zu veroeffentlichen, und ein bereits
    veroeffentlichtes Fenster ist verbraucht. Dessen Messung laeuft weiter, aber es wird kein zweites
    Mal vorgeschlagen – der naechste Short kommt aus dem naechstbesten unbenutzten Ausschnitt.
    """
    for candidate in candidates or []:
        if candidate.get("render_path") and not candidate.get("published_video_id"):
            return candidate
    return None


def youtube_title(video, candidate, already=0):
    """Ein konkreter Titel, ausschliesslich aus belegtem eigenen Material.

    Grundlage ist der eigene Videotitel – unsere eigene Angabe, keine Behauptung. Ein gesungener Satz
    kommt nur davor, wenn er gegen den eigenen Songtext ausgerichtet ist. Eine maschinell erkannte
    Zeile taucht im Titel nie auf; sie koennte verhoert sein.

    Gibt es aus demselben Video schon einen Short, bekommt der naechste eine Ordnungszahl. Zwei
    gleichnamige eigene Videos wuerden sonst gegeneinander laufen – das ist die Kannibalisierung,
    die wir vermeiden wollen. Die Zahl ist eine Zaehlung, keine erfundene Aussage ueber den Inhalt.
    """
    base = " ".join((video.title or "").split())
    if candidate.get("hook") and candidate.get("hook_source") == "aligned":
        title = f"„{candidate['hook'].strip()}“ – {base}"
    else:
        title = f"{base} – {EXCERPT}" + (f" {already+1}" if already else "")
    return title[:TITLE_MAX]


def youtube_description(video, candidate, context=None):
    """Eine Beschreibung nur aus belegten Angaben – oder None, wenn es nichts Belegtes zu sagen gibt.

    Belegt ist: dass dieser Ausschnitt aus unserem eigenen Video stammt, und dessen Adresse. Ein
    Themenkontext kommt nur dazu, wenn er nachweislich geteilt wird. Der Name eines Nachbarvideos
    gehoert nicht hierher.
    """
    lines = [f"Ausschnitt aus „{' '.join((video.title or '').split())}“",
             f"Ganzes Video: https://youtu.be/{video.id}"]
    if context:
        lines.append(" · ".join(context))
    if candidate.get("hook") and candidate.get("hook_source") == "aligned":
        lines.insert(0, candidate["hook"].strip())
    return "\n".join(lines)


def upload_package(video, candidates, context=None):
    """Die fertige Maßnahme: welche Datei, welcher Titel, welche Beschreibung.

    Fehlt die geschnittene Datei, gibt es kein Paket – dann ist die Maßnahme nicht ausfuehrbar und das
    wird gesagt, statt eine Datei zu nennen, die es nicht gibt.
    """
    candidate = selected(candidates)
    if candidate is None:
        return None
    already = sum(1 for c in candidates or [] if c.get("published_video_id"))
    return {"candidate_id": candidate.get("candidate_id"), "file": candidate["render_path"],
            "title": youtube_title(video, candidate, already),
            "description": youtube_description(video, candidate, context),
            "start_seconds": candidate["start_seconds"], "end_seconds": candidate["end_seconds"],
            "duration_seconds": candidate["duration_seconds"],
            "source_video_id": video.id}


def attach_pending(session, today=None):
    """Nachgetragene Video-IDs verbinden, sobald der Sync das neue Video kennt.

    Der Kanalinhaber traegt die ID direkt nach dem Upload ein; unser eigenes Video erscheint aber erst
    mit dem naechsten stuendlichen Sync in der Datenbank. Bis dahin liegt die ID in der Maßnahme und
    wird hier eingehaengt – danach erfasst die Engine die Auslieferung des Shorts wie bei jedem Video.
    """
    from .models import ContentCandidate, GrowthAction, Video
    today = today or date.today()
    linked = []
    rows = session.scalars(select(GrowthAction).where(GrowthAction.action == "produce_for_opportunity"))
    for row in rows:
        payload = row.payload or {}
        # Auch eine bereits vermerkte Veroeffentlichung wird geprueft: fehlt die Zuordnung, weil die
        # Maßnahme vor einer Korrektur gestartet wurde, traegt dieser Lauf sie ohne Zutun nach.
        pending = payload.get("pending_video_id") or payload.get("published_video_id")
        if not pending:
            continue
        if session.get(Video, pending) is None:
            continue                    # Noch nicht synchronisiert – beim naechsten Lauf erneut versuchen.
        # Die mitgefuehrte ID zuerst, dann der Brief, dann dieselbe Auswahl wie bei der Erzeugung.
        candidate_id = (payload.get("pending_candidate_id")
                        or ((payload.get("brief") or {}).get("upload") or {}).get("candidate_id")
                        or candidate_for(session, row.video_id))
        if not candidate_id:
            continue
        candidate = session.get(ContentCandidate, candidate_id)
        if candidate is not None and candidate.published_video_id == pending:
            if payload.get("pending_video_id"):
                row.payload = {**payload, "pending_video_id": None, "published_video_id": pending,
                               "pending_candidate_id": candidate_id}
                session.flush()
            continue                    # Zuordnung steht bereits.
        # Das gemerkte Datum liegt als Zeichenkette in der Nutzlast; die Spalte will ein Datum.
        day = payload.get("pending_day")
        try:
            day = date.fromisoformat(day) if isinstance(day, str) else (day or today)
        except ValueError:
            day = today
        if attach_published(session, candidate_id, pending, day) is None:
            continue
        row.payload = {**payload, "pending_video_id": None, "published_video_id": pending,
                       "pending_candidate_id": candidate_id}
        session.flush()
        linked.append((candidate_id, pending))
    return linked


def candidate_for(session, video_id):
    """Der Kandidat, den die Auswahl fuer dieses Video gewaehlt haette – als Rueckfalloption.

    Wird gebraucht, wenn eine Maßnahme die Kandidaten-ID nicht mitfuehrt: dann bestimmt dieselbe
    Auswahl wie bei der Erzeugung, welcher geschnittene Ausschnitt gemeint war. Geraten wird nichts,
    es ist dieselbe Funktion.
    """
    from .models import Video
    video = session.get(Video, video_id) if video_id else None
    if video is None:
        return None
    chosen = selected(store(session, video, candidates(session, video, None, priors(session)), date.today()))
    return chosen.get("candidate_id") if chosen else None
