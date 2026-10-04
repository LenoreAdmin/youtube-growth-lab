"""Lokale Verarbeitung eigener Originaldateien. Laeuft nie in einer Vercel-Funktion.

Die Mediendateien bleiben auf der eigenen Maschine. Dieses Modul dekodiert sie einmalig, misst
Eigenschaften und schreibt ausschliesslich Zahlen und Zeitmarken nach Neon. Alles, was danach
entschieden wird, liest `app.content` aus der Datenbank – ohne Medienabhaengigkeit.

Gemessen wird, nicht benannt: Energie, Wiederholung, Gesangsanteil, Grenzschaerfe, Bildschnitte,
Helligkeit. Ein semantisches Label wie „Refrain“ entsteht hier nicht.

Werkzeuge, alle offen und lokal: ffmpeg/ffprobe (Dekodieren, Bildschnitte, Rendern), librosa
(Struktur, Energie, Wiederholung), scikit-learn (Gruppierung), optional PySceneDetect (praezisere
Schnitte) und faster-whisper (Text mit Zeitmarken). Fehlt ein optionales Werkzeug, entfallen genau
die Messwerte, die es liefert – der Lauf bricht nicht ab und erfindet keine Ersatzwerte.
"""
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from datetime import date

from .config import settings

log = logging.getLogger("media")

AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SUFFIXES = AUDIO_SUFFIXES | VIDEO_SUFFIXES
SAMPLE_RATE = 22050
MIN_SECTION_SECONDS = 8.0       # Kuerzere Abschnitte taugen nicht als eigener Inhalt.
CUT_SNAP_SECONDS = 1.0          # Ein Startpunkt wird auf einen echten Bildschnitt gezogen, wenn er so nah liegt.
DURATION_TOLERANCE = 1.5        # Zuordnung Datei -> veroeffentlichtes Video nur bei eindeutiger Dauer.
VOCAL_BAND = (200.0, 4000.0)    # Frequenzband, in dem Gesang liegt; der Messwert heisst deshalb Gesangsanteil.
ALIGN_SIMILARITY = 0.6          # Ab dieser Aehnlichkeit gilt eine Zeile als gegen eigenen Text ausgerichtet.


class Unavailable(RuntimeError):
    """Ein notwendiges lokales Werkzeug fehlt. Kein Ersatzwert, kein stiller Weiterlauf."""


def local_only():
    if settings.hosted:
        raise Unavailable("Die Medienverarbeitung laeuft ausschliesslich lokal; gehostet gibt es keine Dateien.")


def _binary(name):
    """ffmpeg/ffprobe aus dem PATH, sonst aus imageio-ffmpeg (als Pip-Paket mitgeliefert)."""
    found = shutil.which(name)
    if found:
        return found
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise Unavailable(f"{name} nicht gefunden. Lokal installieren oder `pip install .[content]`.") from None
    if name == "ffmpeg":
        return exe
    probe = os.path.join(os.path.dirname(exe), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    return probe if os.path.exists(probe) else exe


def _run(args, **kwargs):
    return subprocess.run(args, capture_output=True, check=False, **kwargs)


def digest(path):
    """SHA-256 der Datei: Identitaet des Assets und Schutz gegen doppelte Analyse."""
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            sha.update(block)
    return sha.hexdigest()


def probe(path):
    """Dauer und Spuren der Datei – gemessen, nicht aus dem Dateinamen.

    ffprobe wird bevorzugt. Liegt ffmpeg nur als Pip-Paket vor, gibt es kein ffprobe daneben; dann
    wird derselbe Befund aus der Ausgabe von ffmpeg gelesen.
    """
    probe_binary = _binary("ffprobe")
    if "ffprobe" in os.path.basename(probe_binary).lower():
        out = _run([probe_binary, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
        if out.returncode == 0:
            data = json.loads(out.stdout or b"{}")
            streams = data.get("streams") or []
            return {"duration": float((data.get("format") or {}).get("duration") or 0) or None,
                    "has_video": any(s.get("codec_type") == "video" for s in streams),
                    "has_audio": any(s.get("codec_type") == "audio" for s in streams)}
    return _probe_with_ffmpeg(path)


def _probe_with_ffmpeg(path):
    """Dauer und Spuren aus der Ausgabe von ffmpeg selbst."""
    out = _run([_binary("ffmpeg"), "-hide_banner", "-i", str(path)])
    text = ((out.stderr or b"")+(out.stdout or b"")).decode("utf-8", "replace")
    duration = None
    if "Duration:" in text:
        stamp = text.split("Duration:")[1].split(",")[0].strip()
        try:
            hours, minutes, seconds = stamp.split(":")
            duration = int(hours)*3600+int(minutes)*60+float(seconds)
        except ValueError:
            duration = None
    if duration is None:
        raise Unavailable(f"Die Dauer von {os.path.basename(str(path))} ist nicht lesbar.")
    return {"duration": duration,
            "has_video": "Video:" in text, "has_audio": "Audio:" in text}


def _audio(path):
    """Mono-WAV als Zwischendatei; wird nach der Messung geloescht und nie gespeichert."""
    handle, wav = tempfile.mkstemp(suffix=".wav")
    os.close(handle)
    out = _run([_binary("ffmpeg"), "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                "-ar", str(SAMPLE_RATE), "-f", "wav", wav])
    if out.returncode != 0 or not os.path.getsize(wav):
        os.unlink(wav)
        raise Unavailable("Audio konnte nicht extrahiert werden.")
    return wav


def cuts(path):
    """Bildschnitte in Sekunden. PySceneDetect wenn vorhanden, sonst der Szenenfilter von ffmpeg."""
    try:
        from scenedetect import detect, ContentDetector
        return [round(scene[0].get_seconds(), 3) for scene in detect(str(path), ContentDetector())], "pyscenedetect"
    except Exception:
        pass
    out = _run([_binary("ffmpeg"), "-v", "info", "-i", str(path), "-filter_complex",
                "select='gt(scene,0.4)',metadata=print:file=-", "-an", "-f", "null", "-"])
    found = []
    for line in (out.stdout or b"").decode("utf-8", "replace").splitlines():
        if "pts_time:" in line:
            try:
                found.append(round(float(line.split("pts_time:")[1].split()[0]), 3))
            except (ValueError, IndexError):
                continue
    return sorted(set(found)), "ffmpeg-scene"


def brightness(path, at):
    """Mittlere Helligkeit eines Einzelbildes: ein Pixel Graustufe, direkt aus ffmpeg."""
    out = _run([_binary("ffmpeg"), "-v", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
                "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    if out.returncode != 0 or not out.stdout:
        return None
    return round(out.stdout[0]/255.0, 3)


def structure(wav):
    """Abschnittsgrenzen, Energie, Wiederholung und Gesangsanteil – alles gemessen, nichts benannt."""
    try:
        import librosa
        import numpy as np
    except Exception:
        raise Unavailable("librosa/numpy fehlen. `pip install .[content]`.") from None
    y, sr = librosa.load(wav, sr=SAMPLE_RATE, mono=True)
    if y.size == 0:
        raise Unavailable("Die Audiospur ist leer.")
    total = librosa.get_duration(y=y, sr=sr)
    hop = 512
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    features = np.vstack([librosa.util.normalize(chroma, axis=0), librosa.util.normalize(mfcc, axis=0)])
    # Grenzen aus dem Material selbst: wie viele Abschnitte hineinpassen, entscheidet die Dauer.
    wanted = max(2, min(12, int(total//MIN_SECTION_SECONDS)))
    frames = librosa.segment.agglomerative(features, wanted)
    times = sorted(set([0.0]+[float(t) for t in librosa.frames_to_time(frames, sr=sr, hop_length=hop)]+[total]))
    bounds = [t for t in times if t <= total]
    # Gesangsanteil als Energieanteil im Gesangsband der harmonischen Komponente.
    harmonic = librosa.effects.harmonic(y)
    spectrum = np.abs(librosa.stft(harmonic, hop_length=hop))
    freqs = librosa.fft_frequencies(sr=sr)
    band = (freqs >= VOCAL_BAND[0]) & (freqs <= VOCAL_BAND[1])
    total_energy = spectrum.sum(axis=0)
    vocal_ratio = np.divide(spectrum[band].sum(axis=0), total_energy,
                            out=np.zeros_like(total_energy), where=total_energy > 0)
    sections, vectors = [], []
    for idx in range(len(bounds)-1):
        start, end = bounds[idx], bounds[idx+1]
        if end-start < MIN_SECTION_SECONDS/2:
            continue
        lo = librosa.time_to_frames(start, sr=sr, hop_length=hop)
        hi = max(lo+1, librosa.time_to_frames(end, sr=sr, hop_length=hop))
        lo, hi = max(0, lo), min(features.shape[1], hi)
        if hi <= lo:
            continue
        sections.append({"start_seconds": round(float(start), 3), "end_seconds": round(float(end), 3),
                         "energy": float(rms[lo:hi].mean()) if hi <= len(rms) else float(rms[lo:].mean()),
                         "vocal_presence": round(float(vocal_ratio[lo:min(hi, len(vocal_ratio))].mean()), 3)})
        vectors.append(features[:, lo:hi].mean(axis=1))
    if not sections:
        raise Unavailable("Aus dieser Datei liess sich kein verwertbarer Abschnitt messen.")
    matrix = np.vstack(vectors)
    track_mean = float(rms.mean()) or 1.0
    track_max = float(rms.max()) or 1.0
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    unit = matrix/np.where(norms == 0, 1, norms)
    similarity = unit @ unit.T
    groups = _groups(matrix)
    for idx, section in enumerate(sections):
        others = [similarity[idx][j] for j in range(len(sections)) if j != idx]
        # Nicht die rohe Aehnlichkeit: in einem Musikstueck aehneln sich alle Abschnitte stark, der
        # Rohwert saettigt bei 1 und unterscheidet nichts. Gemessen wird der Kontrast – wie viel mehr
        # dieser Abschnitt seinem naechsten Verwandten gleicht als dem Durchschnitt des Stuecks.
        if others:
            typical = sum(others)/len(others)
            span = 1.0-typical
            contrast = (max(others)-typical)/span if span > 1e-6 else 0.0
            section["repetition_strength"] = round(max(0.0, min(1.0, float(contrast))), 3)
        else:
            section["repetition_strength"] = 0.0
        section["repetition_group"] = groups[idx]
        section["energy_rel"] = round(section["energy"]/track_mean, 3)
        section["energy"] = round(section["energy"]/track_max, 3)
        # Grenzschaerfe: wie stark sich das Material an der Grenze zum Vorgaenger unterscheidet.
        section["novelty"] = round(float(1.0-similarity[idx][idx-1]), 3) if idx > 0 else None
    return {"duration": round(float(total), 3), "sections": sections}


def _groups(matrix):
    """Messbare Gruppierung aehnlicher Abschnitte – eine Nummer, kein Name."""
    try:
        from sklearn.cluster import AgglomerativeClustering
        count = max(2, min(len(matrix)//2, 4))
        if len(matrix) <= count:
            return list(range(len(matrix)))
        labels = AgglomerativeClustering(n_clusters=count).fit_predict(matrix)
        return [int(label) for label in labels]
    except Exception:
        return [None]*len(matrix)


def transcript(wav, lyrics=None):
    """Text mit Zeitmarken. Nur so sicher, wie es tatsaechlich ist.

    Ohne faster-whisper gibt es keinen Text – und damit keinen zitierten Hook, aber auch keine
    Erfindung. Liegt eigener Text vor (eine `.txt` neben der Datei), werden die erkannten Segmente
    dagegen ausgerichtet: stimmt eine Zeile ausreichend ueberein, gilt sie als `aligned` und ist
    zitierfaehig. Alles andere bleibt `asr` mit seiner gemessenen Sicherheit.
    """
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return [], None
    rows = []
    try:
        import librosa
        model = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
        # Das Audio wird selbst dekodiert und als Array uebergeben. Sonst dekodiert faster-whisper
        # ueber PyAV, dessen Aufrufsignatur sich zwischen Versionen aendert.
        samples, _ = librosa.load(wav, sr=16000, mono=True)
        segments, info = model.transcribe(samples, vad_filter=True, word_timestamps=False)
        for segment in segments:
            text = (segment.text or "").strip()
            if not text:
                continue
            confidence = (round(float(math.exp(segment.avg_logprob)), 3)
                          if segment.avg_logprob is not None else None)
            rows.append({"start_seconds": round(float(segment.start), 3),
                         "end_seconds": round(float(segment.end), 3),
                         "text": text[:1000], "confidence": confidence, "source": "asr"})
    except Exception as exc:
        # Ein defektes optionales Werkzeug darf nur seine eigenen Messwerte kosten, nicht den Lauf.
        log.warning("transcript unavailable: %s: %s", type(exc).__name__, str(exc)[:200])
        return [], None
    if lyrics:
        rows = _align(rows, lyrics)
    return rows, {"language": getattr(info, "language", None)}


def _align(rows, lyrics):
    """Erkannte Segmente gegen den eigenen Text ausrichten; nur ausreichende Treffer gelten als Fakt."""
    from difflib import SequenceMatcher
    wanted = [line.strip() for line in lyrics.splitlines() if line.strip()]
    if not wanted:
        return rows
    cursor = 0
    for row in rows:
        best, score = None, 0.0
        for offset, line in enumerate(wanted[cursor:cursor+4]):
            ratio = SequenceMatcher(None, row["text"].lower(), line.lower()).ratio()
            if ratio > score:
                best, score = (cursor+offset, line), ratio
        if best and score >= ALIGN_SIMILARITY:
            row["text"], row["source"], row["confidence"] = best[1][:1000], "aligned", round(score, 3)
            cursor = best[0]+1
    return rows


def _lyrics_for(path):
    """Eigener Songtext, wenn er als `.txt` neben der Datei liegt. Keine gepflegten Zeitmarken."""
    candidate = os.path.splitext(str(path))[0]+".txt"
    if os.path.exists(candidate):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None
    return None


def analyse(path):
    """Eine Datei vollstaendig messen. Was ein fehlendes Werkzeug nicht liefert, bleibt leer."""
    local_only()
    info = probe(path)
    if not info["has_audio"]:
        raise Unavailable("Ohne Audiospur gibt es keine Struktur zu messen.")
    wav = _audio(path)
    lines = []
    tools = {"ffmpeg": os.path.basename(_binary("ffmpeg"))}
    try:
        measured = structure(wav)
        tools["librosa"] = _version("librosa")
        lines, speech = transcript(wav, _lyrics_for(path))
        if lines:
            tools["faster_whisper"] = _version("faster_whisper")
            tools["language"] = (speech or {}).get("language")
    finally:
        os.unlink(wav)
    sections = measured["sections"]
    found, detector = ([], None)
    if info["has_video"]:
        found, detector = cuts(path)
        tools["cuts"] = detector
    for section in sections:
        if found:
            nearest = min(found, key=lambda t: abs(t-section["start_seconds"]))
            section["boundary_cut_distance"] = round(abs(nearest-section["start_seconds"]), 3)
            # Ein Short beginnt auf einem echten Schnitt, wenn einer nah genug liegt.
            if section["boundary_cut_distance"] <= CUT_SNAP_SECONDS:
                section["start_seconds"] = nearest
            inside = [t for t in found if section["start_seconds"] <= t <= section["end_seconds"]]
            section["visual_cuts"] = len(inside)
            span = max(0.001, section["end_seconds"]-section["start_seconds"])
            section["visual_cut_density"] = round(len(inside)/span, 3)
        if info["has_video"]:
            section["brightness"] = brightness(path, section["start_seconds"]+0.2)
    return {"duration": measured["duration"] or info["duration"], "has_video": info["has_video"],
            "has_audio": info["has_audio"], "sections": sections, "lines": lines,
            "tools": tools}


def _version(module):
    try:
        return __import__(module).__version__
    except Exception:
        return "unbekannt"


def match_video(session, duration):
    """Zuordnung zum veroeffentlichten Video ueber die Dauer – eindeutig oder gar nicht.

    Geraten wird hier nichts: passen keine oder mehrere Videos, bleibt das Asset ohne Zuordnung und
    die Growth Engine arbeitet weiter ohne Inhaltswissen statt mit falschem.
    """
    from sqlalchemy import select
    from .models import Video
    if not duration:
        return None, "Datei ohne lesbare Dauer – keine Zuordnung."
    rows = [v for v in session.scalars(select(Video))
            if v.duration_seconds and abs(float(v.duration_seconds)-float(duration)) <= DURATION_TOLERANCE]
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"Kein veroeffentlichtes Video mit {duration:.1f} s Dauer (Toleranz {DURATION_TOLERANCE} s)."
    return None, ("Mehrdeutig: "+", ".join(v.title for v in rows[:4])+" haben praktisch dieselbe Dauer.")


def ingest(session, path, today=None, reanalyse=False):
    """Eine Datei einmalig verarbeiten und ausschliesslich Messwerte speichern.

    Unveraenderte Dateien werden nicht erneut analysiert: der SHA-256 ist der Schluessel.
    """
    from sqlalchemy import delete, select
    from .models import MediaAsset, ContentSection, ContentLine, ContentCandidate
    local_only()
    today = today or date.today()
    asset_id = digest(path)
    existing = session.get(MediaAsset, asset_id)
    if existing is not None and existing.status == "analysed" and not reanalyse:
        return {"asset_id": asset_id, "status": "unchanged", "path": str(path),
                "video_id": existing.video_id, "sections": None}
    try:
        measured = analyse(path)
    except Unavailable as exc:
        asset = existing or MediaAsset(id=asset_id, path=str(path))
        asset.path, asset.status, asset.note = str(path), "error", str(exc)[:1000]
        asset.bytes = os.path.getsize(path)
        session.add(asset)
        session.flush()
        return {"asset_id": asset_id, "status": "error", "path": str(path), "note": str(exc)}
    video, note = match_video(session, measured["duration"])
    asset = existing or MediaAsset(id=asset_id, path=str(path))
    asset.path, asset.bytes = str(path), os.path.getsize(path)
    asset.duration_seconds = measured["duration"]
    asset.has_video, asset.has_audio = measured["has_video"], measured["has_audio"]
    asset.video_id = video.id if video else None
    asset.status, asset.note, asset.tools = "analysed", note, measured["tools"]
    session.add(asset)
    session.flush()
    session.execute(delete(ContentSection).where(ContentSection.asset_id == asset_id))
    session.execute(delete(ContentLine).where(ContentLine.asset_id == asset_id))
    # Neu gemessene Grenzen ergeben neue Fenster. Noch nicht veroeffentlichte Kandidaten der alten
    # Messung sind damit ungueltig und werden entfernt; veroeffentlichte bleiben, denn sie tragen die
    # Verbindung zur tatsaechlichen Distribution und damit das Gelernte.
    session.execute(delete(ContentCandidate).where(ContentCandidate.asset_id == asset_id,
                                                   ContentCandidate.published_video_id.is_(None)))
    for idx, section in enumerate(measured["sections"]):
        session.add(ContentSection(asset_id=asset_id, idx=idx,
                                   **{k: section.get(k) for k in
                                      ("start_seconds", "end_seconds", "energy", "energy_rel",
                                       "repetition_strength", "repetition_group", "vocal_presence",
                                       "novelty", "visual_cuts", "visual_cut_density", "brightness",
                                       "boundary_cut_distance")}))
    for idx, line in enumerate(measured["lines"]):
        session.add(ContentLine(asset_id=asset_id, idx=idx, start_seconds=line["start_seconds"],
                                end_seconds=line["end_seconds"], text=line["text"],
                                confidence=line.get("confidence"), source=line.get("source", "asr")))
    session.flush()
    return {"asset_id": asset_id, "status": "analysed", "path": str(path),
            "video_id": asset.video_id, "note": note, "sections": len(measured["sections"]),
            "lines": len(measured["lines"]), "duration": measured["duration"],
            "title": (video.title if video else None)}


def render(session, candidate_id, out_dir):
    """Den Kandidaten als fertige Datei schneiden – exakt die gespeicherten Zeiten, ffmpeg, lokal.

    Hochladen bleibt menschlich: der YouTube-Zugriff ist read-only und bleibt es.
    """
    from .models import ContentCandidate, MediaAsset
    local_only()
    row = session.get(ContentCandidate, candidate_id)
    if row is None:
        raise Unavailable(f"Kandidat {candidate_id} existiert nicht.")
    asset = session.get(MediaAsset, row.asset_id)
    if asset is None or not os.path.exists(asset.path):
        raise Unavailable("Die Originaldatei liegt nicht am gespeicherten Pfad.")
    os.makedirs(out_dir, exist_ok=True)
    suffix = ".mp4" if asset.has_video else ".m4a"
    out = os.path.join(out_dir, f"short-{candidate_id}-{int(row.start_seconds)}s{suffix}")
    args = [_binary("ffmpeg"), "-y", "-v", "error", "-ss", f"{row.start_seconds:.3f}",
            "-to", f"{row.end_seconds:.3f}", "-i", asset.path]
    if asset.has_video:
        # Hochformat fuer den Shorts-Feed, ohne zu beschneiden: einpassen und auffuellen.
        args += ["-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
                        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "20", "-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-vn", "-c:a", "aac", "-b:a", "192k"]
    args.append(out)
    result = _run(args)
    if result.returncode != 0 or not os.path.exists(out):
        raise Unavailable("ffmpeg konnte den Ausschnitt nicht schreiben.")
    row.render_path = out
    session.flush()
    return out


def masters(directory):
    """Alle Originaldateien im Master-Verzeichnis, stabil sortiert."""
    found = []
    for root, _, files in os.walk(directory):
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in SUFFIXES:
                found.append(os.path.join(root, name))
    return sorted(found)
