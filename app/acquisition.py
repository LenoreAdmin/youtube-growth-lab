"""Active organic traffic engine: find real audience surfaces, act on them, measure the views they send.

The product goal is additional legitimate organic views. This module is the acquisition layer; V3–V6
stay measurement and learning infrastructure. Every surface here names a place that can be checked:

- `own_external_referrer` – an external page that already sent viewers (own analytics, EXT_URL detail),
  verified over HTTP so a dead link is never proposed,
- `recommending_video` / `recommending_channel` – a video or channel whose audience already reaches us
  (own analytics, RELATED_VIDEO detail plus stored public metadata),
- `own_search_intent` – a search term our own analytics reported, with measured views.

What is deliberately NOT here: web or forum search for new communities. That needs a search provider
with an API key, which this sprint excludes. Surfaces are therefore limited to places that already
touch the channel – measured, not guessed. Nothing is ever posted automatically; YouTube stays
read-only and outreach is prepared for a human.
"""
import logging
import re
from math import log10
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse
from sqlalchemy import func, select
from .backfill import upsert
from .budget import SyncBudgetExceeded
from .config import settings
from .history import pacific_day, lag_days
from .models import (AudiencePool, ChannelPlaylist, DiscoveryChannel, DiscoveryItem, DiscoveryRun, DiscoverySignal, GrowthAction,
                     TrafficDaily, TrafficSurface, Video, utcnow)

log = logging.getLogger(__name__)
VERSION = "acquisition-v7"
SIGNAL_WINDOW_DAYS = 90
MIN_SURFACE_VIEWS = 1        # Sichtbar wird eine Fläche ab einem gemessenen View von dort.
MIN_ACTIONABLE_VIEWS = 5     # Vorgeschlagen wird erst, wenn dort in 90 Tagen wirklich Publikum war.
MIN_CANDIDATE_VIEWS = 2000   # Öffentliche Reichweite, ab der ein fremdes Video als Fläche zählt.
MIN_CANDIDATE_SUBSCRIBERS = 500
MIN_SHARED_TOKENS = 2        # Ein gemeinsames Wort ist keine Themengleichheit (siehe V6).
MIN_EXPECTED_WEEKLY_VIEWS = 0.35  # Darunter ist eine Quelle eine Beobachtung, keine Traffic-Aktion.
# PRODUKTREGEL (harte Sperre, nicht verhandelbar und nicht durch Lernen aufhebbar):
#
# Wer unsere Musik schon selbst ausgewaehlt, gespielt oder aufgenommen hat – Radios, Redaktionen, Medien,
# Kuratoren, einbettende Seiten –, ist eine bestehende Beziehung. Diese Beziehungen sind gewachsen, ohne
# dass dieses System daran beteiligt war, und sie sind mehr wert als jeder Vorschlag, den es erzeugen
# koennte. Sie werden ausschliesslich gemessen: ihre Trafficdaten tragen das Lernen ueber unser Publikum,
# aber sie loesen niemals eine Ansprache aus. Ein einzelner unpassender Automatik-Vorschlag kann eine
# solche Beziehung zerstoeren; kein moeglicher Zugewinn wiegt das auf.
#
# Praktisch: jede Flaeche, von der wir bereits gemessenen Traffic bekommen, ist geschuetzt. Ziel dieses
# Systems sind neue YouTube-Audiences, die uns noch nicht kennen. Im Zweifel wird nicht kontaktiert.
PROTECTED_KINDS = {"own_external_referrer", "embed_site", "recommending_video", "recommending_channel"}
PROTECTED_NOTE = ("Bestehende Quelle: von dort kommen bereits Zuschauer. Sie wird gemessen und fliesst in das "
                  "Audience-Lernen ein, wird aber nie angesprochen – eine gewachsene Beziehung darf durch eine "
                  "automatische Empfehlung nicht beschaedigt werden.")
# Beweiskraft der Audience-Naehe, absteigend. Fuer YT_OTHER_PAGE (Kanaele) verlangen wir mindestens
# „topic“; blosse Wortueberlappung hat gar keine Klasse und kommt nicht durch.
FIT_RANK = {"neighbourhood": 3, "genre": 2, "topic": 1}
FIT_LABELS = {"neighbourhood": "belegte Nachbarschaft", "genre": "gemeinsames Genre/Stil",
              "topic": "inhaltliche Themennaehe"}
TEASER_SECONDS = 90         # Kuerzer ist ein Teaser, kein vollstaendiges Musikvideo.
# Themennaehe ohne musikalische Evidenz ist eine schwaechere Spur und darf nicht oben stehen.
FIT_FACTOR = {"neighbourhood": 1.0, "genre": 1.0, "topic": 0.8}
ANCHOR_FACTOR = {"neighbourhood": 1.0, "semantic": 1.0, "style": 0.85}
# Eine Flaeche, bei der nur das Thema geteilt ist, bleibt eine Hypothese – auch mit 1,13 Millionen
# Abonnenten. Reichweite darf fehlende musikalische Naehe nicht ersetzen (Fall Travel With Koushik).
TOPIC_ONLY_CAP = 30.0


def fit_weight(fit):
    """Wie stark wiegt dieser Fit? Ein gemeinsamer Ort wiegt mehr als eine gemeinsame Stilschublade."""
    return FIT_FACTOR[fit["class"]]*ANCHOR_FACTOR.get(fit.get("anchor") or "semantic", 1.0)


def capped(score, fit):
    """Ohne belegte musikalische Naehe kann eine Flaeche nicht aussehen wie eine starke Chance."""
    return min(score, TOPIC_ONLY_CAP) if fit.get("class") == "topic" else score
# Wer in eine Playlist aufgenommen wird, wird dort gespielt; ein Kommentar laedt nur zum Klick ein.
# Das ist ein Unterschied im Mechanismus, nicht in der Sympathie – und er gehoert in die Reihenfolge.
MECHANISM_FACTOR = {"curated_playlist": 1.0, "pool_channel": 0.85}
MIN_PLAYLIST_ITEMS = 5       # Unter fuenf Titeln ist es kein gepflegter Ort, sondern ein Entwurf.
# Formatwörter beschreiben die Verpackung, nicht das Thema: „Album Teaser“ passt auf jedes Album-Teaser
# der Welt. Der erste Lauf hat darüber ein BLACKPINK-Video als Fläche für unser Teaser-Video vorgeschlagen.
FORMAT_WORDS = {"album", "teaser", "single", "trailer", "visual", "preview", "snippet", "clip", "extended",
                "instrumental", "karaoke", "cover", "medley", "bonus", "deluxe", "release", "promo", "playlist",
                "compilation", "collection", "megamix", "session", "sessions", "acoustic", "unplugged"}
GENERIC_SHARE = 0.05         # Ein Begriff in mehr als 5 % aller fremden Titel ist ein Formatwort, kein Thema.
GENERIC_MIN_CORPUS = 50
HYPOTHESIS_CAP = 60.0         # Ein unbelegter Mechanismus kommt nicht ueber diesen Wert.
UNPROVEN_FACTOR = 0.85
MAX_VERIFY_PER_RUN = 8       # Bounded HTTP-Prüfung je Lauf; der Rest folgt am nächsten Tag.
VERIFY_TTL_DAYS = 7
QUEUE_LIMIT = 3
MIN_TRIALS_FOR_WEIGHT = 3    # Unter drei ausgewerteten Versuchen wird nichts als bewiesen behandelt.
RETIRE_AFTER = 3             # Drei ausgewertete Versuche ohne zusätzliche Views: Quelle wird verworfen.
WEIGHT_RANGE = (0.7, 1.3)
WINDOW_DAYS = 14
# Welche Traffic-Quellen eine Zielmetrik umfasst. Zwei Maßnahmen an einem Video sind nur erlaubt,
# wenn die Quelle der neuen Maßnahme nicht in der Zielmetrik einer laufenden liegt.
DISCOVERY_SOURCES = {"YT_SEARCH", "RELATED_VIDEO", "YT_CHANNEL", "SUBSCRIBER", "NOTIFICATION", "END_SCREEN",
                     "PLAYLIST", "YT_PLAYLIST_PAGE", "YT_OTHER_PAGE"}
# Impressionen entstehen dort, wo unser Thumbnail auf einer YouTube-Oberflaeche gezeigt wird. Ein Klick aus
# einem Kommentar oder von einer fremden Seite ist keine Impression – solche Quellen sind davon trennbar.
IMPRESSION_SOURCES = DISCOVERY_SOURCES-{"YT_OTHER_PAGE"}
ALL_SOURCES = DISCOVERY_SOURCES | {"EXT_URL", "SHORTS", "ADVERTISING", "NO_LINK_OTHER", "NO_LINK_EMBEDDED"}
# impressions_7d zaehlt nur Auslieferungen auf YouTube-Oberflaechen: ein externer Link erzeugt Views,
# aber keine Thumbnail-Impression. Deshalb ist externe Ansprache davon trennbar.
METRIC_SOURCES = {"discovery_views_7d": DISCOVERY_SOURCES, "impressions_7d": IMPRESSION_SOURCES,
                  "playlist_views_7d": {"PLAYLIST", "YT_PLAYLIST_PAGE"},
                  "external_views_7d": {"EXT_URL"}, "search_views_7d": {"YT_SEARCH"},
                  "suggested_views_7d": {"RELATED_VIDEO"}, "other_views_7d": {"YT_OTHER_PAGE", "NO_LINK_OTHER"}}
SURFACE_KINDS = {
    "own_external_referrer": {"traffic_source": "EXT_URL", "lever_class": "external_outreach",
                              "action": "reach_out_to_referrer", "metric": "external_views_7d"},
    "recommending_channel": {"traffic_source": "RELATED_VIDEO", "lever_class": "community_participation",
                             "action": "engage_recommending_channel", "metric": "suggested_views_7d"},
    "recommending_video": {"traffic_source": "RELATED_VIDEO", "lever_class": "community_participation",
                           "action": "engage_recommending_video", "metric": "suggested_views_7d"},
    "own_search_intent": {"traffic_source": "YT_SEARCH", "lever_class": "search_wording",
                          "action": "serve_search_intent", "metric": "search_views_7d"},
    # Neu gefundene Flächen: Publikum, das uns noch nicht erreicht, dessen Existenz aber öffentlich belegt ist.
    "candidate_video": {"traffic_source": "YT_OTHER_PAGE", "lever_class": "community_participation",
                        "action": "engage_candidate_video", "metric": "other_views_7d"},
    "candidate_channel": {"traffic_source": "YT_OTHER_PAGE", "lever_class": "community_participation",
                          "action": "engage_candidate_channel", "metric": "other_views_7d"},
    # Aussderhalb von YouTube: eine oeffentliche Seite, auf der die Zielgruppe zusammenkommt.
    # Neu gefundene Audiences, die uns noch nicht kennen – ueber die vorhandenen YouTube-APIs entdeckt.
    "curated_playlist": {"traffic_source": "PLAYLIST", "lever_class": "playlist_placement",
                         "action": "pitch_to_playlist_curator", "metric": "playlist_views_7d"},
    "pool_channel": {"traffic_source": "YT_OTHER_PAGE", "lever_class": "community_participation",
                     "action": "engage_pool_channel", "metric": "other_views_7d"},
    # Fremde Seiten, die unser Video schon einbetten: gemessen in eigenen Analytics, ansprechbar.
    "embed_site": {"traffic_source": "EXT_URL", "lever_class": "external_outreach",
                   "action": "reach_out_to_embed_site", "metric": "external_views_7d"},
}
# Plattformen, über die Menschen Links privat weiterschicken. Sie sind Beleg dafür, dass geteilt wird,
# aber keine Stelle, die man ansprechen kann – „whatsapp.com kontaktieren“ wäre ein Phantom-Auftrag.
SHARE_PLATFORMS = {"whatsapp.com", "t.co", "twitter.com", "x.com", "facebook.com", "l.facebook.com", "m.facebook.com",
                   "instagram.com", "l.instagram.com", "telegram.org", "web.telegram.org", "google.com",
                   "news.google.com", "youtube.com", "m.youtube.com", "youtu.be", "linkedin.com", "pinterest.com",
                   "tiktok.com", "snapchat.com", "discord.com", "mail.google.com", "outlook.com", "bing.com",
                   "duckduckgo.com", "yandex.ru", "baidu.com", "messenger.com", "reddit.com", "linktr.ee"}
ACTION_LABELS = {"reach_out_to_referrer": "Bestehende externe Quelle – nur Messsignal, keine Ansprache",
                 "pitch_to_playlist_curator": "Kurator einer fremden Playlist ansprechen",
                 "engage_pool_channel": "Neu gefundenen Kanal mit passender Audience erreichen",
                 "reach_out_to_embed_site": "Seite bettet unser Video ein – bestehende Quelle, keine Ansprache",
                 "engage_candidate_video": "Bei einem reichweitenstarken passenden Video sichtbar werden",
                 "engage_candidate_channel": "Kanal mit belegter Reichweite echt beteiligen",
                 "engage_recommending_channel": "Kanal mit passender Audience echt beteiligen",
                 "engage_recommending_video": "Beim empfehlenden Video sichtbar werden",
                 "serve_search_intent": "Belegte Suchintention im Wortlaut bedienen"}
NO_SPAM = ("Keine Spam-Posts, keine Bots, keine gekauften Views, keine Engagement-Pods, keine irreführenden "
           "Beiträge. Community-Regeln lesen und einhalten; nur dort auftreten, wo das Thema wirklich passt. "
           "Das System postet nichts automatisch.")


# ----------------------------------------------------------------------------- helpers
def _latest_signals(session, kind):
    """Latest analytics window per video: {video_id: {detail: (views, watch_minutes)}}."""
    out, ends = defaultdict(dict), {}
    for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.kind == kind)):
        if row.video_id not in ends or row.window_end > ends[row.video_id]:
            ends[row.video_id] = row.window_end
            out[row.video_id] = {}
        if row.window_end == ends[row.video_id]:
            out[row.video_id][row.detail] = (row.views, row.watch_minutes)
    return out, ends


def as_url(detail):
    """Analytics reports external referrers as a host or URL; make a fetchable https URL of it."""
    text = (detail or "").strip()
    if not text or text.lower() in ("other", "unknown", "no link"):
        return None
    if not text.startswith(("http://", "https://")):
        text = "https://"+text
    parsed = urlparse(text)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return parsed.geturl()


def adjacency_access(views, public_reach, minimum, how, what):
    """Auch eine empfehlende Quelle liefert bereits Zuschauer: gemessen ja, angesprochen nein.

    Ob YouTube uns dort algorithmisch ausliefert oder ob dort jemand unsere Musik selbst aufgenommen hat,
    laesst sich von aussen nicht sicher unterscheiden. Genau deshalb gilt hier die Zweifelsregel.
    """
    if views > 0:
        return protected_access(views, what.rstrip(".") if what else "Diese Quelle",
                                "Neue Audiences werden ueber die YouTube-Suche gefunden, nicht bei Quellen, "
                                "die uns schon ausliefern.")
    return _adjacency_access(views, public_reach, minimum, how, what)


def _adjacency_access(views, public_reach, minimum, how, what):
    """Belegte Nachbarschaft mit oeffentlich nachpruefbarem Publikum – aber kein Ersatz fuer Zufluss.

    Frueher war ein einziger gemessener View hier handelbar, sobald die Flaeche gross war. Das ergab
    Vorschlaege mit rund 0,1 erwarteten Views pro Woche als wichtigste Traffic-Aktion. Eine Flaeche,
    von der praktisch nichts kommt, bleibt ein Beleg fuer Naehe – und eine Aufgabe wird daraus erst,
    wenn der gemessene Zufluss eine Groessenordnung hat, die man auswerten kann.
    """
    weekly = expected_weekly_views(views)
    if views < MIN_ACTIONABLE_VIEWS or weekly < MIN_EXPECTED_WEEKLY_VIEWS:
        reach = f" Die Fläche selbst ist groß ({public_reach} Publikum) und bleibt als Beleg für Nähe stehen." \
            if public_reach and public_reach >= minimum else ""
        return {"actionable": False, "rules": NO_SPAM, "manual": True, "evidence_grade": "adjacency_only",
                "why_not": (f"Von dort kamen {views} Views in 90 Tagen, also rund {weekly} Views pro Woche. Unter "
                            f"{MIN_EXPECTED_WEEKLY_VIEWS} Views pro Woche ist das keine Trafficquelle, sondern eine "
                            f"Beobachtung: der Effekt einer Maßnahme wäre nicht messbar.{reach}")}
    return {"actionable": True, "rules": NO_SPAM, "manual": True, "how": how,
            "evidence_grade": "adjacency_plus_public_reach",
            "caveat": (f"Bisher kam von dort {views} Views in 90 Tagen – die Nachbarschaft ist belegt, der Zufluss "
                       f"aber klein. {what} Erwartung entsprechend klein halten und am Ergebnis messen.")}


def protected_access(views, what, detail=""):
    """Eine bestehende Quelle: Messsignal, kein Outreach-Ziel. Siehe PRODUKTREGEL oben."""
    return {"actionable": False, "protected": True, "rules": NO_SPAM, "manual": True,
            "protected_reason": "protected_existing_source",
            "why_not": (f"{what} hat uns bereits {views} Views geschickt. {PROTECTED_NOTE}"
                        + (f" {detail}" if detail else ""))}


def measured_access(views, how):
    """Gemessene Flächen brauchen einen belastbaren Traffic-Pfad, sonst bleiben sie Beleg statt Aufgabe."""
    if views < MIN_ACTIONABLE_VIEWS:
        return {"actionable": False, "rules": NO_SPAM, "manual": True,
                "why_not": (f"Nur {views} Views in 90 Tagen von dieser Fläche: zu wenig für einen belastbaren "
                            f"Traffic-Pfad. Ab {MIN_ACTIONABLE_VIEWS} Views wird daraus eine Aufgabe.")}
    return {"actionable": True, "rules": NO_SPAM, "manual": True, "how": how}


def referrer_access(url, views):
    """Eine Seite, die uns schon Zuschauer schickt, ist eine bestehende Beziehung – kein Outreach-Ziel."""
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if host not in SHARE_PLATFORMS and "." in host and not host.startswith("android-app"):
        return {**protected_access(views, f"Die Seite {host}"), "platform": host}
    if host in SHARE_PLATFORMS or host.startswith("android-app") or "." not in host:
        return {"actionable": False, "platform": host,
                "why_not": (f"{host} ist eine Weiterleitungs- oder Teilen-Plattform, keine Redaktion: dort gibt es "
                            "niemanden, den man ansprechen kann. Der Eintrag bleibt als Beleg, dass Zuschauer den "
                            "Link privat weitergeben."),
                "rules": NO_SPAM, "manual": True}
    if views < MIN_ACTIONABLE_VIEWS:
        return {"actionable": False, "platform": host,
                "why_not": (f"Nur {views} Views in 90 Tagen von {host}: zu wenig für einen belastbaren Traffic-Pfad. "
                            f"Ab {MIN_ACTIONABLE_VIEWS} Views wird daraus eine Aufgabe."),
                "rules": NO_SPAM, "manual": True}
    return {"actionable": True, "platform": host,
            "how": (f"Die Seite {host} verlinkt oder erwähnt uns bereits. Kontakt über Impressum, Kontaktformular "
                    "oder die dort genannte Adresse suchen und den konkreten Themenbezug nennen."),
            "rules": NO_SPAM, "manual": True}


def verify(url, client=None):
    """Is that page actually reachable? A surface nobody can visit is not a surface."""
    try:
        import httpx
        opener = client or httpx.Client(timeout=6.0, follow_redirects=True,
                                        headers={"User-Agent": "youtube-growth-lab/1.0 (+read-only link check)"})
        try:
            response = opener.head(url)
            if response.status_code >= 400:
                response = opener.get(url)
            return response.status_code
        finally:
            if client is None:
                opener.close()
    except Exception as exc:                      # Netzfehler ist kein Beleg gegen die Seite, nur fehlende Prüfung.
        log.info("Link check failed for one surface (%s)", type(exc).__name__)
        return None


def source_views(session, video_id, source, start, end):
    """Organic views from exactly one traffic source in a day window – the attribution basis."""
    total = session.scalar(select(func.coalesce(func.sum(TrafficDaily.views), 0)).where(
        TrafficDaily.video_id == video_id, TrafficDaily.source == source, TrafficDaily.paid.is_(False),
        TrafficDaily.day >= start, TrafficDaily.day <= end)) or 0
    minutes = session.scalar(select(func.coalesce(func.sum(TrafficDaily.watch_minutes), 0)).where(
        TrafficDaily.video_id == video_id, TrafficDaily.source == source, TrafficDaily.paid.is_(False),
        TrafficDaily.day >= start, TrafficDaily.day <= end)) or 0
    return {"views": int(total), "watch_minutes": float(minutes), "start": str(start), "end": str(end), "source": source}


# ----------------------------------------------------------------------------- learning from real traffic
def weights(session):
    """What has actually produced additional qualified views so far? Bounded, honest, per surface kind.

    Below MIN_TRIALS_FOR_WEIGHT nothing is treated as proven: the weight stays neutral and says so.
    A kind that failed RETIRE_AFTER times without any additional views is retired – the engine then
    spends its attention on alternatives instead of repeating it.
    """
    record = {}
    for row in session.scalars(select(GrowthAction).where(GrowthAction.version == VERSION,
                                                          GrowthAction.status == "evaluated")):
        kind = (row.payload or {}).get("surface_kind") or "unbekannt"
        entry = record.setdefault(kind, {"trials": 0, "with_traffic": 0, "views_gained": 0, "views_lost": 0})
        gained = ((row.evaluation or {}).get("attribution") or {}).get("views_delta")
        entry["trials"] += 1
        if gained is not None and gained > 0:
            entry["with_traffic"] += 1
            entry["views_gained"] += gained
        elif gained is not None and gained < 0:
            entry["views_lost"] += -gained
    out = {}
    for kind, entry in record.items():
        low, high = WEIGHT_RANGE
        if entry["trials"] >= MIN_TRIALS_FOR_WEIGHT:
            rate = entry["with_traffic"]/entry["trials"]
            entry["weight"] = round(low+(high-low)*rate, 3)
            entry["basis"] = (f"{entry['with_traffic']} von {entry['trials']} Versuchen brachten messbar zusätzliche "
                              f"Views aus dieser Quelle (+{entry['views_gained']} insgesamt).")
            entry["retired"] = entry["with_traffic"] == 0 and entry["trials"] >= RETIRE_AFTER
        else:
            entry["weight"], entry["retired"] = 1.0, False
            entry["basis"] = (f"Nur {entry['trials']} ausgewertete Versuche (< {MIN_TRIALS_FOR_WEIGHT}): "
                              "nichts bewiesen, Gewicht bleibt neutral.")
        out[kind] = entry
    return out


def lever_record(session):
    """Was hat ein Hebel bisher wirklich gebracht? Unbelegt heisst unbelegt – kein Vertrauensvorschuss."""
    record = {}
    for row in session.scalars(select(GrowthAction).where(GrowthAction.version == VERSION,
                                                          GrowthAction.status == "evaluated")):
        lever = row.lever_class or "unbekannt"
        entry = record.setdefault(lever, {"trials": 0, "with_traffic": 0, "views_gained": 0})
        gained = ((row.evaluation or {}).get("attribution") or {}).get("views_delta")
        entry["trials"] += 1
        if gained and gained > 0:
            entry["with_traffic"] += 1
            entry["views_gained"] += gained
    for lever in set(list(record)+[spec["lever_class"] for spec in SURFACE_KINDS.values()]):
        entry = record.setdefault(lever, {"trials": 0, "with_traffic": 0, "views_gained": 0})
        proven = entry["trials"] >= MIN_TRIALS_FOR_WEIGHT and entry["with_traffic"] > 0
        entry["proven"] = proven
        entry["status"] = "belegt" if proven else "hypothese"
        low, high = WEIGHT_RANGE
        entry["weight"] = (round(low+(high-low)*entry["with_traffic"]/max(1, entry["trials"]), 3) if proven
                           else UNPROVEN_FACTOR)
        entry["basis"] = (f"{entry['with_traffic']} von {entry['trials']} ausgewerteten Versuchen brachten "
                          f"attribuierte Views (+{entry['views_gained']})." if entry["trials"] else
                          "Noch kein ausgewerteter Versuch: der Mechanismus ist für diesen Kanal unbelegt.")
        entry["upgrade_rule"] = ("Erst attribuierte Views aus dieser Quelle werten den Hebel auf; "
                                f"dafür braucht es mindestens {MIN_TRIALS_FOR_WEIGHT} ausgewertete Versuche.")
    return record


def mechanism_status(surface, levers):
    """Ist der Wirkmechanismus für diesen Kanal gemessen oder eine Hypothese?"""
    lever = levers.get(surface.lever_class, {})
    measured = (surface.evidence or {}).get("measured_views_90d") or 0
    if lever.get("proven"):
        return {"status": "belegt", "why": lever.get("basis")}
    if measured >= MIN_ACTIONABLE_VIEWS:
        return {"status": "teilweise belegt",
                "why": (f"Aus dieser Quelle kamen real {measured} Views – dass sie sich durch eine Ansprache "
                        "erhöhen lassen, ist noch unbelegt."),
                "upgrade_rule": lever.get("upgrade_rule")}
    return {"status": "hypothese",
            "why": ("Für diesen Kanal ist nicht gemessen, dass dieser Hebel zusätzliche Views erzeugt. "
                    "Die Maßnahme ist ein Test dieser Hypothese, kein bewiesener Mechanismus."),
            "upgrade_rule": lever.get("upgrade_rule")}


def scoreboard(session):
    """The only number this system is judged by: additional attributed views per source, measured."""
    rows = list(session.scalars(select(GrowthAction).where(GrowthAction.version == VERSION)))
    per_source, total = {}, 0
    for row in rows:
        attribution = ((row.evaluation or {}).get("attribution") or {}) if row.status == "evaluated" else {}
        delta = attribution.get("views_delta")
        if delta is None:
            continue
        entry = per_source.setdefault(row.traffic_source or "unbekannt",
                                      {"views_delta": 0, "evaluated": 0, "watch_minutes_delta": 0.0})
        entry["views_delta"] += delta
        entry["watch_minutes_delta"] += attribution.get("watch_minutes_delta") or 0
        entry["evaluated"] += 1
        total += delta
    return {"attributed_views_delta": total, "per_source": per_source,
            "proposed": sum(1 for r in rows if r.status == "proposed"),
            "running": sum(1 for r in rows if r.status == "running"),
            "evaluated": sum(1 for r in rows if r.status == "evaluated"),
            "note": ("Beobachtete Veränderung der Views aus genau dieser Quelle im Messfenster gegenüber dem "
                     "gleich langen Fenster davor; Werbetage zählen nicht mit. Beobachtung, kein Kausalbeweis.")}


# ----------------------------------------------------------------------------- surfaces
def expected_weekly_views(measured_views, window_days=SIGNAL_WINDOW_DAYS):
    """Conservative: what that surface already delivers per week is the anchor, not a wish."""
    per_week = measured_views/max(1, window_days)*7
    return round(per_week, 2)


def score_candidate(shared_tokens, item_views, subscribers, weight=1.0):
    """Öffentlich belegte Reichweite, bewusst mit Abschlag: dieses Publikum hat uns noch nicht erreicht."""
    fit = min(1.0, len(shared_tokens)/3)
    reach = min(1.0, log10((item_views or 0)+1)/6)
    subs = min(1.0, log10((subscribers or 0)+1)/5)
    raw = 100*(0.30*fit + 0.30*reach + 0.10*subs + 0.10*0.45)*0.8
    return round(max(0.0, min(100.0, raw*weight)), 1)


MAX_VOCAB_TOKENS = 30


def own_vocabulary(session):
    """Unser Thema in Worten – und zwar nicht nur aus dem eigenen Titel.

    Ein Titel wie „Sealand Trainstories“ ergibt nach Stoppwörtern ein einziges Wort; damit ist keine
    Themenähnlichkeit prüfbar. Die tragfähigere Quelle sind die Videos, neben die YouTube uns tatsächlich
    stellt und aus denen messbar Zuschauer kamen: deren Titel beschreiben unsere Nachbarschaft belegt.
    """
    from .discovery import tokens as split
    vocab = {}
    for video in session.scalars(select(Video).where(Video.active.is_(True))):
        vocab[video.id] = set(split(video.title))
    for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.kind == "own_search_term")):
        vocab.setdefault(row.video_id, set()).update(split(row.detail))
    titles = {row.video_id: row.title for row in session.scalars(select(DiscoveryItem))}
    counts = defaultdict(lambda: defaultdict(int))
    for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.kind == "own_suggested_source")):
        title = titles.get(row.detail)
        if not title or row.views < 1:
            continue
        for token in split(title):
            counts[row.video_id][token] += row.views
    for video_id, weighted in counts.items():
        top = sorted(weighted.items(), key=lambda kv: -kv[1])[:MAX_VOCAB_TOKENS]
        vocab.setdefault(video_id, set()).update(token for token, _ in top)
    # Die eigenen Angaben zum Video gehoeren dazu – aber nur die Begriffe, die ein Thema tragen duerfen.
    # Marken-, Release- und Formatwoerter bleiben draussen, sonst waere „album“ wieder ein Thema.
    from . import audience
    for video in session.scalars(select(Video).where(Video.active.is_(True))):
        for row in audience.profile_terms(session, video):
            if row["category"] in audience.TOPIC_CATEGORIES:
                vocab.setdefault(video.id, set()).add(row["term"])
    return {video_id: set(list(words)[:MAX_VOCAB_TOKENS*3]) for video_id, words in vocab.items()}


def existing_sources(session):
    """Alles, von dem wir schon gemessenen Traffic bekommen – die geschuetzten bestehenden Beziehungen.

    Diese Mengen sind die technische Form der PRODUKTREGEL: was hier drin steht, wird gemessen und
    gelernt, aber nie angesprochen.
    """
    hosts, videos, channels, titles = set(), set(), set(), set()
    for kind in ("own_external", "own_embed"):
        for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.kind == kind)):
            url = as_url(row.detail) or ""
            host = urlparse(url).netloc.lower()
            host = host[4:] if host.startswith("www.") else host
            if host:
                hosts.add(host)
    items = {row.video_id: row for row in session.scalars(select(DiscoveryItem))}
    for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.kind == "own_suggested_source")):
        videos.add(row.detail)
        item = items.get(row.detail)
        if item is not None:
            if item.channel_id:
                channels.add(item.channel_id)
            if item.channel_title:
                titles.add(item.channel_title.strip().lower())
    return {"hosts": hosts, "videos": videos, "channels": channels, "titles": titles}


def protected_pool(pool, existing):
    """Ist dieser gefundene Ort in Wahrheit eine bestehende Beziehung – oder nicht sicher auszuschliessen?"""
    details = pool.details or {}
    title = (pool.channel_title or pool.title or "").strip().lower()
    if pool.kind == "channel":
        if pool.key in existing["channels"] or title in existing["titles"]:
            return ("Dieser Kanal liefert uns bereits Zuschauer: bestehende Quelle, nur Messsignal. "
                    + PROTECTED_NOTE)
    if pool.kind == "playlist":
        if pool.channel_id in existing["channels"] or title in existing["titles"]:
            return ("Der Betreiber dieser Playlist liefert uns bereits Zuschauer: bestehende Beziehung. "
                    + PROTECTED_NOTE)
        contains = details.get("contains_own")
        if contains:
            return ("Diese Playlist fuehrt unsere Musik bereits: eine bestehende Platzierung. Sie wird "
                    "gemessen, aber ihr Kurator wird nicht angeschrieben. "+PROTECTED_NOTE)
        if contains is None:
            return ("Ob diese Playlist unsere Musik schon fuehrt, ist noch nicht geprueft. Solange das offen "
                    "ist, wird sie nicht angeschrieben – im Zweifel nicht kontaktieren.")
    return None


def video_profiles(session, generic=None):
    """Das musikalische Profil je eigenem Video – einmal je Lauf, damit jede Pruefung dieselbe Basis hat."""
    from . import audience
    generic = generic_tokens(session) if generic is None else generic
    return {video.id: audience.music_profile(session, video, generic)
            for video in session.scalars(select(Video).where(Video.active.is_(True)))}


def full_release(video):
    """Ein vollstaendiges Musikvideo – kein Teaser, kein Schnipsel."""
    from . import audience
    from .discovery import tokens as split
    words = set(split(video.title))
    return (video.duration_seconds or 0) >= TEASER_SECONDS and not (words & (FORMAT_WORDS | audience.LABEL_WORDS))


def best_video_for(session, words, profiles, prefer_full=False, tags=(), topics=(), intent=None,
                   neighbourhood=()):
    """Welches unserer Videos passt inhaltlich wirklich zu diesem Ort – und ist ueberhaupt vorzeigbar?

    Fuer eine Playlist-Anfrage ist ein 45-Sekunden-Teaser der falsche Kandidat, selbst wenn seine Worte
    zufaellig passen: der Kurator soll etwas aufnehmen koennen. Deshalb entscheidet die Passung, und bei
    aehnlicher Passung das vollstaendige Video.
    """
    from . import audience
    best, best_key = None, None
    for video in session.scalars(select(Video).where(Video.active.is_(True))):
        profile = profiles.get(video.id)
        if profile is None:
            continue
        fit = audience.audience_fit(words, profile, intent_hit=intent if (intent or {}).get("video_id") == video.id
                                    else None, neighbourhood=neighbourhood, topics=topics, tags=tags)
        if fit["class"] is None:
            continue
        complete = 1 if full_release(video) else 0
        # Fuer eine Playlist-Anfrage entscheidet zuerst, ob es ueberhaupt ein vollstaendiges Video ist:
        # ein Album-Teaser laesst sich nicht in eine Playlist aufnehmen, auch wenn seine Worte passen.
        key = ((complete, FIT_RANK[fit["class"]], len(fit["shared"])) if prefer_full
               else (FIT_RANK[fit["class"]], len(fit["shared"]), complete))+(video.duration_seconds or 0,)
        if best_key is None or key > best_key:
            best, best_key = (video, fit), key
    return best


def generic_tokens(session):
    """Begriffe, die in vielen fremden Titeln stehen, taugen nicht als Themenbeleg – datengetrieben ermittelt."""
    from .discovery import tokens as split
    counts, total = defaultdict(int), 0
    for item in session.scalars(select(DiscoveryItem)):
        total += 1
        for token in set(split(item.title)):
            counts[token] += 1
    if total < GENERIC_MIN_CORPUS:
        return set(FORMAT_WORDS)
    frequent = {token for token, count in counts.items() if count/total > GENERIC_SHARE}
    return frequent | FORMAT_WORDS


def candidate_matches(session, vocab, generic=None, profiles=None):
    """Fremde Videos aus den Suchproben, die belegbar zur Audience eines eigenen Videos passen.

    Wortueberlappung allein genuegt nicht. „Long Ride“ von Young Nudy teilte mit uns long, out und ride –
    das ist eine Sprache, keine Zielgruppe. Gefordert ist ein Fit nach app/audience.py: gemeinsames Genre
    mit musikalischer Klassifizierung, belegte Nachbarschaft oder mindestens zwei Begriffe mit erkennbarer
    Bedeutung.
    """
    from . import audience
    from .discovery import tokens as split
    generic = generic if generic is not None else generic_tokens(session)
    profiles = profiles if profiles is not None else video_profiles(session, generic)
    existing = existing_sources(session)
    matches = []
    for item in session.scalars(select(DiscoveryItem)):
        if not (item.via or {}).get("queries") or item.video_id in vocab:
            continue
        # PRODUKTREGEL: Quellen, die uns schon Zuschauer schicken, sind Messsignale und keine Ziele.
        if item.video_id in existing["videos"] or item.channel_id in existing["channels"] \
                or (item.channel_title or "").strip().lower() in existing["titles"]:
            continue
        words = set(split(item.title))|{t for tag in (item.tags or []) for t in split(tag)}
        best, fit = None, None
        for video_id, own in vocab.items():
            profile = profiles.get(video_id)
            if profile is None:
                continue
            # Bewusst die vollstaendigen Worte des Kandidaten: der Fit prueft selbst, was davon zu uns
            # gehoert, und braucht den Rest, um zu erkennen, ob der Ort ueberhaupt musikalisch ist.
            candidate = audience.audience_fit(words | (own & words), profile, tags=item.tags or [])
            if candidate["class"] is None:
                continue
            if fit is None or FIT_RANK[candidate["class"]] > FIT_RANK[fit["class"]] \
                    or (FIT_RANK[candidate["class"]] == FIT_RANK[fit["class"]]
                        and len(candidate["shared"]) > len(fit["shared"])):
                best, fit = video_id, candidate
        if best is not None:
            matches.append((item, best, tuple(fit["shared"]), fit))
    return matches


ACTIVITY_PATTERNS = ((r"([\d.,]+)\s*(?:k\s*)?(?:members|mitglieder|subscribers|abonnenten)", "members"),
                     (r"([\d.,]+)\s*(?:posts|beitr\w+|messages)", "posts"),
                     (r"([\d.,]+)\s*(?:replies|antworten|kommentare|comments)", "replies"),
                     (r"([\d.,]+)\s*(?:topics|themen|threads)", "threads"),
                     (r"([\d.,]+)\s*(?:views|aufrufe)", "views"))


def specific(tokens_in, generic):
    """Begriffe, die ueberhaupt etwas ueber ein Thema sagen: keine Marken, keine Formate, keine Fuellwoerter."""
    from . import audience
    from .discovery import BRAND
    return sorted({t for t in tokens_in if t not in generic and t not in BRAND and len(t) >= 4
                   and t not in audience.HELPER_WORDS and t not in audience.LABEL_WORDS})


def pool_size(pool):
    if pool.kind == "playlist":
        return f"{pool.item_count if pool.item_count is not None else '?'} Titel"
    return f"{pool.subscribers if pool.subscribers is not None else '?'} Abonnenten"


def collect_pools(session, vocab, learned, generic, store, profiles=None):
    """Gefundene Playlists und Kanaele pruefen: passt das Thema wirklich, ist der Ort gepflegt, wie gross ist er?

    Zwei Wege zur Relevanz. Erstens mindestens zwei spezifische gemeinsame Begriffe. Zweitens - staerker -
    die Playlist enthaelt ein Video aus einem Kanal, neben dem YouTube uns schon ausliefert: dann kuratiert
    dort jemand genau unsere Nachbarschaft.

    Jede Entscheidung wird begruendet und am Kandidaten gespeichert. Ein leerer Trichter ohne Begruendung
    ist nicht auswertbar; mit Begruendung ist er die Arbeitsliste fuer die naechste Verbesserung.
    """
    from . import audience
    from .discovery import tokens as split
    neighbour_titles = {row.title for row in session.scalars(select(DiscoveryItem)) if row.title}
    profiles = video_profiles(session, generic) if profiles is None else profiles
    existing = existing_sources(session)
    verdicts = []
    for pool in session.scalars(select(AudiencePool)):
        details = pool.details or {}
        haystack = " ".join([pool.title or "", pool.description or "", details.get("keywords") or ""]
                            + list(details.get("items") or []) + list(details.get("topics") or []))
        words = set(split(haystack))
        best, shared = None, []
        for video_id, own in vocab.items():
            common = specific(words & own, generic)
            if len(common) > len(shared):
                best, shared = video_id, common
        overlap = [title for title in (details.get("items") or []) if title in neighbour_titles]
        # Die Query-Evidenz ist Teil des Relevanznachweises: wir haben nach einem belegten eigenen Thema
        # gesucht. Trifft der Kandidat diesen Themenkopf, ist der Bezug belegt – auch wenn er mit unserem
        # Videotitel kein Wort teilt („Mongolian Trains“ vs. „Sealand Trainstories“).
        intent = details.get("intent") or {}
        hit = audience.matches(intent, words) if intent.get("head") else None
        # Welches unserer Videos passt hier wirklich – und ist es vorzeigbar? Fuer eine Playlist-Anfrage
        # ist ein Album-Teaser der falsche Kandidat, auch wenn seine Worte zufaellig passen.
        chosen = best_video_for(session, sorted(words), profiles, prefer_full=True,
                               tags=details.get("items") or [], topics=details.get("topics") or [],
                               intent=intent, neighbourhood=overlap[:2])
        fit = chosen[1] if chosen else audience.audience_fit(sorted(words), next(iter(profiles.values()), {
            "genres": set(), "moods": set(), "places": set(), "topics": set(), "terms": {}}))
        if chosen:
            best = chosen[0].id
            shared = sorted(set(shared) | set(fit["shared"]))
        verdict = {"kind": pool.kind, "title": pool.title, "url": pool.url, "query": pool.query,
                   "query_source": details.get("query_source"), "size": pool_size(pool),
                   "shared_tokens": shared, "neighbourhood_overlap": overlap[:2], "kept": False, "reason": None,
                   "intent": intent.get("label"), "intent_hit": (hit or {}).get("head"),
                   "fit_class": fit["class"], "fit_why": fit["why"],
                   "video": chosen[0].title if chosen else None}

        def decide(reason, kept=False):
            verdict["reason"], verdict["kept"] = reason, kept
            verdicts.append(verdict)
            pool.details = {**details, "verdict": {"kept": kept, "reason": reason, "shared_tokens": shared,
                                                  "fit_class": fit["class"]}}
            return kept

        guard = protected_pool(pool, existing)
        if guard and (best is not None and fit["class"] is not None):
            decide(guard)
            continue
        if best is None or fit["class"] is None:
            detail = (f" Der Audience-Intent „{intent['label']}“ traegt hier nicht: "
                      f"{', '.join(intent.get('head') or [])} fehlt im Titel, in der Beschreibung und im Inhalt."
                      if intent.get("head") and not hit else "")
            decide(fit["why"]+detail)
            continue
        if pool.kind == "channel" and FIT_RANK[fit["class"]] < FIT_RANK["topic"]:
            decide("Fuer einen Kanal verlangen wir Genre-, Stil- oder Nachbarschaftsevidenz; "
                   "gemeinsame Allerweltswoerter genuegen nicht.")
            continue
        if pool.kind == "playlist":
            if not full_release(chosen[0]):
                decide(f"Fuer eine Playlist-Anfrage fehlt ein vollstaendiges Musikvideo mit Passung: "
                       f"„{chosen[0].title}“ ist ein Teaser oder Ausschnitt, und ein Kurator kann nur ein "
                       f"ganzes Stueck aufnehmen.")
                continue
            if (pool.item_count or 0) < MIN_PLAYLIST_ITEMS:
                decide(f"Kein gepflegter Ort: {pool.item_count or 0} Titel, mindestens {MIN_PLAYLIST_ITEMS} noetig.")
                continue
            entry = learned.get("curated_playlist", {})
            if entry.get("retired"):
                decide("Hebel „Playlist-Platzierung“ ist nach erfolglosen Versuchen zurueckgestellt.")
                continue
            reason = (f"Die Playlist „{pool.title}“ von „{pool.channel_title}“ enthält {pool.item_count} Titel. "
                      f"Audience-Fit ({FIT_LABELS[fit['class']]}): {fit['why']}"
                      + (f" Passendes eigenes Video: „{chosen[0].title}“." if chosen else "")
                      + (f" Gesucht wurde über den Intent „{hit['label']}“." if hit else "")
                      + " Wer die Playlist pflegt, kuratiert für genau diese Zuschauer.")
            decide(f"Aufgenommen ({FIT_LABELS[fit['class']]}): {fit['why']}", kept=True)
            store("curated_playlist", pool.key, best, pool.title, pool.url,
                  {"item_count": pool.item_count, "owner": pool.channel_title, "owner_subscribers": pool.subscribers,
                   "shared_tokens": shared, "neighbourhood_overlap": overlap[:3], "query": pool.query,
                   "sample_items": (details.get("items") or [])[:5], "intent": intent or None,
                   "audience_fit": fit, "demand_source": "public_youtube_search", "why": reason,
                   "uncertainty": ("mittel: Größe und Inhalt sind öffentlich belegt, ob der Kurator reagiert und ob "
                                   "daraus Views entstehen, ist offen.")},
                  {"traffic_potential": capped(score_candidate(shared or ["nachbarschaft", "belegt"], pool.views,
                                                               pool.subscribers,
                                                               entry.get("weight", 1.0)*fit_weight(fit)
                                                               * MECHANISM_FACTOR["curated_playlist"]), fit),
                   "expected_weekly_views": None, "item_count": pool.item_count, "owner_subscribers": pool.subscribers,
                   "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit; kein gemessener eigener Traffic."},
                  {"actionable": True, "rules": NO_SPAM, "manual": True,
                   "how": ("Den Kanal des Kurators öffnen, die Kontaktmöglichkeit suchen und sachlich fragen, ob das "
                           "Video in die Playlist passt – mit Link zum Video, ohne Druck und ohne Gegenleistung.")})
        elif pool.kind == "channel":
            if (pool.subscribers or 0) < MIN_CANDIDATE_SUBSCRIBERS:
                decide(f"Zu kleine oder verborgene Reichweite: {pool.subscribers if pool.subscribers is not None else 'keine Angabe'}"
                       f" Abonnenten, mindestens {MIN_CANDIDATE_SUBSCRIBERS} noetig.")
                continue
            entry = learned.get("pool_channel", {})
            if entry.get("retired"):
                decide("Hebel „Community-Teilnahme“ ist nach erfolglosen Versuchen zurueckgestellt.")
                continue
            decide(f"Aufgenommen ({FIT_LABELS[fit['class']]}): {fit['why']}", kept=True)
            store("pool_channel", pool.key, best, pool.title, pool.url,
                  {"subscribers": pool.subscribers, "video_count": pool.item_count, "views": pool.views,
                   "shared_tokens": shared, "topics": (details.get("topics") or [])[:4], "query": pool.query,
                   "country": details.get("country"), "demand_source": "public_youtube_search",
                   "intent": intent or None, "audience_fit": fit,
                   "why": (f"„{pool.title}“ hat {pool.subscribers} Abonnenten. Audience-Fit "
                           f"({FIT_LABELS[fit['class']]}): {fit['why']}"
                           + (f" Passendes eigenes Video: „{chosen[0].title}“." if chosen else "")
                           + " Dieses Publikum kennt uns nicht."),
                   "uncertainty": "mittel: Kanalgröße öffentlich belegt, eigener Zufluss nicht gemessen."},
                  {"traffic_potential": capped(score_candidate(shared, pool.views, pool.subscribers,
                                                               entry.get("weight", 1.0)*fit_weight(fit)
                                                               * MECHANISM_FACTOR["pool_channel"]), fit),
                   "expected_weekly_views": None, "subscribers": pool.subscribers,
                   "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit; kein gemessener eigener Traffic."},
                  {"actionable": True, "rules": NO_SPAM, "manual": True,
                   "how": ("Die neueste thematisch passende Veröffentlichung ansehen und inhaltlich kommentieren; "
                           "bei klarer Nähe eine sachliche Anfrage über die angegebene Kontaktmöglichkeit.")})
        else:
            decide(f"Unbekannte Pool-Art „{pool.kind}“.")
    return verdicts


def collect_embed_sites(session, learned, store, now, http=None):
    """Seiten, die unser Video einbetten: gemessener eigener Traffic und ein Ort, den man ansprechen kann."""
    embeds, ends = _latest_signals(session, "own_embed")
    checked = 0
    for video_id, details in embeds.items():
        for detail, (views, minutes) in sorted(details.items(), key=lambda kv: -kv[1][0]):
            url = as_url(detail)
            if url is None or views < MIN_SURFACE_VIEWS:
                continue
            entry = learned.get("embed_site", {})
            if entry.get("retired"):
                continue
            status_code = verify(url, http) if checked < MAX_VERIFY_PER_RUN else None
            checked += 1
            if status_code is not None and status_code >= 400:
                continue
            host = urlparse(url).netloc
            store("embed_site", url, video_id, host, url,
                  {"measured_views_90d": views, "measured_watch_minutes_90d": round(minutes, 1),
                   "window_end": str(ends.get(video_id)), "demand_source": "own_analytics",
                   "why": (f"{host} bettet unser Video ein: {views} Views und {round(minutes)} Minuten "
                           "Wiedergabezeit in 90 Tagen kamen von dort. Dort ist Publikum, das uns schon sieht."),
                   "verified": status_code is not None, "http_status": status_code},
                  {"traffic_potential": score_surface(1.0, views, 0.8, 0.3, entry.get("weight", 1.0)),
                   "expected_weekly_views": expected_weekly_views(views),
                   "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit."},
                  referrer_access(url, views), status_code, now)


def collect_candidates(session, vocab, learned, store):
    """Neue Flächen, mehrfach bestätigt: mindestens zwei gemeinsame Begriffe und zwei verschiedene Kanäle.

    Die Reichweite ist öffentlich nachprüfbar (Views, Abonnenten); dass dieses Publikum uns erreicht, ist
    ausdrücklich nicht gemessen. Genau deshalb der Abschlag in der Bewertung und der Proxy-Hinweis.
    """
    generic = generic_tokens(session)
    profiles = video_profiles(session, generic)
    matches = candidate_matches(session, vocab, generic, profiles)
    channels = {row.channel_id: row for row in session.scalars(select(DiscoveryChannel))}
    seen_channels = set()
    for item, video_id, shared, fit in sorted(matches, key=lambda m: (-FIT_RANK[m[3]["class"]], -(m[0].views or 0))):
        # Korroboration auf Themenebene: andere Treffer desselben Videos, die mindestens zwei Begriffe
        # mit diesem teilen. Identische Wortmengen zu verlangen waere zu streng – Titel sind verschieden.
        related = [(other, other_channel) for other, other_video, other_shared, _ in matches
                   for other_channel in [other.channel_id]
                   if other_video == video_id and len(set(other_shared) & set(shared)) >= MIN_SHARED_TOKENS]
        members = len(related)
        distinct = len({channel for _, channel in related if channel})
        if members < 2 or distinct < 2:
            continue        # Ein einzelner Treffer ist Zufall, nicht ein Thema.
        channel = channels.get(item.channel_id)
        subscribers = channel.subscribers if channel else None
        corroboration = (f"{members} Videos aus {distinct} verschiedenen Kanälen teilen die Begriffe "
                         f"{', '.join(shared)} mit unserem Video. Audience-Fit: "
                         f"{FIT_LABELS[fit['class']]} – {fit['why']}")
        if (item.views or 0) >= MIN_CANDIDATE_VIEWS and not learned.get("candidate_video", {}).get("retired"):
            store("candidate_video", item.video_id, video_id, item.title,
                  f"https://www.youtube.com/watch?v={item.video_id}",
                  {"public_views": item.views, "channel": item.channel_title, "subscribers": subscribers,
                   "shared_tokens": list(shared), "members": members, "channels": distinct,
                   "audience_fit": fit,
                   "demand_source": "public_proxy_corroborated", "found_via": (item.via or {}).get("queries", [])[:3],
                   "generic_words_ignored": sorted(generic & set(shared)) or None,
                   "why": (f"Dieses Video hat {item.views} öffentlich gezählte Views und liegt thematisch neben uns: "
                           f"{corroboration} Sein Publikum ist belegt vorhanden – es erreicht uns nur noch nicht."),
                   "uncertainty": "mittel: Reichweite öffentlich belegt, eigener Zufluss noch nicht gemessen."},
                  {"traffic_potential": capped(score_candidate(shared, item.views, subscribers,
                                                               learned.get("candidate_video", {}).get("weight", 1.0)
                                                               * fit_weight(fit)), fit),
                   "expected_weekly_views": None, "public_views": item.views,
                   "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit; kein gemessener eigener Traffic."},
                  {"actionable": True, "rules": NO_SPAM, "manual": True,
                   "how": ("Das Video ansehen und einen inhaltlichen Kommentar hinterlassen, der ohne Link Wert hat. "
                           "Keine Eigenwerbung, kein Link, keine Wiederholung.")})
        if (channel is not None and (subscribers or 0) >= MIN_CANDIDATE_SUBSCRIBERS
                and channel.channel_id not in seen_channels
                and not learned.get("candidate_channel", {}).get("retired")):
            seen_channels.add(channel.channel_id)
            store("candidate_channel", channel.channel_id, video_id, channel.title,
                  f"https://www.youtube.com/channel/{channel.channel_id}",
                  {"subscribers": subscribers, "video_count": channel.video_count, "shared_tokens": list(shared),
                   "members": members, "channels": distinct, "demand_source": "public_proxy_corroborated",
                   "audience_fit": fit,
                   "why": (f"„{channel.title}“ hat {subscribers} Abonnenten und veröffentlicht thematisch nahe Videos: "
                           f"{corroboration} Dort ist ein Publikum, das zu unserem Video passt."),
                   "uncertainty": "mittel: Kanalgröße öffentlich belegt, eigener Zufluss noch nicht gemessen."},
                  {"traffic_potential": capped(score_candidate(shared, channel.views, subscribers,
                                                               learned.get("candidate_channel", {}).get("weight", 1.0)
                                                               * fit_weight(fit)), fit),
                   "expected_weekly_views": None, "subscribers": subscribers,
                   "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit; kein gemessener eigener Traffic."},
                  {"actionable": True, "rules": NO_SPAM, "manual": True,
                   "how": ("Beim Kanal echt teilnehmen: neueste thematisch passende Veröffentlichung ansehen und "
                           "inhaltlich kommentieren; bei klarer Nähe eine sachliche Anfrage über die angegebene "
                           "Kontaktmöglichkeit.")})


def score_surface(fit, present_views, access_score, effort, weight):
    """Relative traffic potential for this channel (0–100) – not a probability.

    Deliberately dominated by measured audience presence and realistic access, so an analytically
    interesting surface without a traffic path cannot reach the top of the queue.
    """
    present = min(1.0, present_views/25.0)
    # Multiplikativ: ohne gemessenes Publikum bleibt auch eine perfekt passende Fläche unten.
    raw = 100*(0.35*fit + 0.25*access_score + 0.10*(1-effort))*(0.2+0.8*present)
    return round(max(0.0, min(100.0, raw*weight)), 1)


def collect(session, now=None, budget=None, http=None):
    """Daily pass: turn measured analytics detail into concrete, verifiable audience surfaces."""
    now = now or utcnow()
    today = pacific_day(now)
    learned = weights(session)
    levers = lever_record(session)
    videos = {v.id: v for v in session.scalars(select(Video).where(Video.active.is_(True)))}
    items = {row.video_id: row for row in session.scalars(select(DiscoveryItem))}
    channels = {row.channel_id: row for row in session.scalars(select(DiscoveryChannel))}
    external, ends = _latest_signals(session, "own_external")
    owned_http = None
    if http is None and external:
        # Eine Verbindung je Lauf statt einer je Link: auf Serverless zaehlt jede Sekunde.
        try:
            import httpx
            owned_http = http = httpx.Client(timeout=6.0, follow_redirects=True,
                                             headers={"User-Agent": "youtube-growth-lab/1.0 (+read-only link check)"})
        except Exception:
            owned_http = http = None
    suggested, _ = _latest_signals(session, "own_suggested_source")
    searched, _ = _latest_signals(session, "own_search_term")
    written, verified = 0, 0
    per_kind = defaultdict(int)

    def store(kind, key, video_id, title, url, evidence, scores, access, http_status=None, verified_at=None):
        nonlocal written
        spec = SURFACE_KINDS[kind]
        # Ein unbelegter Mechanismus darf nicht aussehen wie eine belegte Quelle: sichtbar gekennzeichnet
        # und mit Abschlag bewertet, bis attribuierte Views ihn aufwerten.
        lever = levers.get(spec["lever_class"], {})
        potential = (scores or {}).get("traffic_potential")
        if potential is not None:
            proven = bool(lever.get("proven"))
            factor = lever.get("weight", UNPROVEN_FACTOR)
            scores = {**scores, "traffic_potential": round(min(100.0 if proven else HYPOTHESIS_CAP,
                                                              potential*factor), 1),
                      "mechanism": lever.get("status", "hypothese"),
                      "adjusted_by": (f"Hebel {spec['lever_class']}: {lever.get('status', 'hypothese')} "
                                      f"(Faktor {factor}{'' if proven else f', Deckel {HYPOTHESIS_CAP:.0f}'})")}
        statement = upsert(session, TrafficSurface).values(
            day=today, kind=kind, key=str(key)[:300], video_id=video_id, title=str(title)[:300],
            url=url[:500] if url else None, traffic_source=spec["traffic_source"], lever_class=spec["lever_class"],
            evidence=evidence, scores=scores, access=access, http_status=http_status,
            verified_at=verified_at, status="open")
        session.execute(statement.on_conflict_do_update(index_elements=["day", "kind", "key"], set_={
            "video_id": statement.excluded.video_id, "title": statement.excluded.title, "url": statement.excluded.url,
            "evidence": statement.excluded.evidence, "scores": statement.excluded.scores,
            "access": statement.excluded.access, "http_status": statement.excluded.http_status,
            "verified_at": statement.excluded.verified_at}))
        written += 1
        per_kind[kind] += 1

    # Nach Tag sortiert, damit der Cache den juengsten Pruefstand einer Flaeche traegt.
    known = {(r.kind, r.key): r for r in session.scalars(select(TrafficSurface).order_by(TrafficSurface.day))}
    # Der heutige Stand wird komplett neu erhoben: eine Flaeche, die die Regeln nicht mehr besteht, soll
    # verschwinden und nicht als Altbestand einen Vorschlag am Leben halten.
    for stale in list(session.scalars(select(TrafficSurface).where(TrafficSurface.day == today))):
        session.delete(stale)
    session.flush()

    # ---- externe Seiten, die bereits Zuschauer schicken
    for video_id, details in external.items():
        for detail, (views, minutes) in sorted(details.items(), key=lambda kv: -kv[1][0]):
            if budget:
                budget.check()
            if views < MIN_SURFACE_VIEWS:
                continue
            url = as_url(detail)
            if url is None:
                continue
            previous = known.get(("own_external_referrer", url))
            status, checked = (previous.http_status, previous.verified_at) if previous else (None, None)
            stale = checked is None or (now-checked.replace(tzinfo=checked.tzinfo or now.tzinfo)).days >= VERIFY_TTL_DAYS
            if stale and verified < MAX_VERIFY_PER_RUN:
                status, checked, verified = verify(url, http), now, verified+1
            if status is not None and status >= 400:
                continue        # Tote Seite ist keine Fläche.
            host = urlparse(url).netloc
            weekly = expected_weekly_views(views)
            entry = learned.get("own_external_referrer", {})
            if entry.get("retired"):
                continue
            store("own_external_referrer", url, video_id, host, url,
                  {"measured_views_90d": views, "measured_watch_minutes_90d": round(minutes, 1),
                   "window_end": str(ends.get(video_id)), "demand_source": "own_analytics",
                   "why": (f"{views} Views und {round(minutes)} Minuten Wiedergabezeit kamen in 90 Tagen real von "
                           f"{host} auf dieses Video – dort ist bereits Publikum, das uns anklickt."),
                   "verified": status is not None, "http_status": status},
                  {"traffic_potential": score_surface(1.0, views, 0.8, 0.3, entry.get("weight", 1.0)),
                   "expected_weekly_views": weekly, "components": {"fit": 1.0, "present_views": views,
                                                                   "access": 0.8, "effort": 0.3},
                   "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit."},
                  referrer_access(url, views), status, checked)

    # ---- Videos und Kanäle, die uns bereits empfehlen
    for video_id, details in suggested.items():
        for detail, (views, minutes) in sorted(details.items(), key=lambda kv: -kv[1][0]):
            if budget:
                budget.check()
            if views < MIN_SURFACE_VIEWS or not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(detail)):
                continue
            item = items.get(detail)
            channel = channels.get(item.channel_id) if item else None
            entry = learned.get("recommending_video", {})
            if not entry.get("retired"):
                store("recommending_video", detail, video_id,
                      (item.title if item else f"Video {detail}"), f"https://www.youtube.com/watch?v={detail}",
                      {"measured_views_90d": views, "measured_watch_minutes_90d": round(minutes, 1),
                       "demand_source": "own_analytics", "channel": item.channel_title if item else None,
                       "neighbour_views": item.views if item else None,
                       "why": (f"YouTube hat unser Video {views} mal neben „{item.title if item else detail}“ "
                               "ausgeliefert und Zuschauer haben geklickt – dieses Publikum erreicht uns schon.")},
                      {"traffic_potential": max(score_surface(0.9, views, 0.6, 0.4, entry.get("weight", 1.0)),
                                                score_candidate(["nachbarschaft", "belegt"], item.views if item else 0,
                                                                None, entry.get("weight", 1.0))),
                       "expected_weekly_views": expected_weekly_views(views),
                       "public_views": item.views if item else None,
                       "components": {"fit": 0.9, "present_views": views, "access": 0.6, "effort": 0.4},
                       "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit."},
                      adjacency_access(views, item.views if item else None, MIN_CANDIDATE_VIEWS,
                                       "Unter diesem Video als Kanal echt teilnehmen: das Video ansehen und einen "
                                       "inhaltlichen Kommentar schreiben, der ohne Link Wert hat. Kein "
                                       "Eigenwerbe-Link, keine Wiederholung.",
                                       f"Das Nachbarvideo selbst hat {item.views if item else '?'} öffentliche Views."))
            if channel is not None and channel.subscribers:
                kind_entry = learned.get("recommending_channel", {})
                if kind_entry.get("retired"):
                    continue
                store("recommending_channel", channel.channel_id, video_id, channel.title,
                      f"https://www.youtube.com/channel/{channel.channel_id}",
                      {"measured_views_90d": views, "subscribers": channel.subscribers,
                       "video_count": channel.video_count, "demand_source": "own_analytics",
                       "why": (f"Über Videos von „{channel.title}“ ({channel.subscribers} Abonnenten) kamen real "
                               f"{views} Views auf unser Video – die Audience dieses Kanals überschneidet sich mit unserer.")},
                      {"traffic_potential": max(score_surface(0.85, views, 0.5, 0.5, kind_entry.get("weight", 1.0)),
                                                score_candidate(["nachbarschaft", "belegt"], channel.views,
                                                                channel.subscribers, kind_entry.get("weight", 1.0))),
                       "expected_weekly_views": expected_weekly_views(views), "subscribers": channel.subscribers,
                       "components": {"fit": 0.85, "present_views": views, "access": 0.5, "effort": 0.5},
                       "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit."},
                      adjacency_access(views, channel.subscribers, MIN_CANDIDATE_SUBSCRIBERS,
                                       "Beim Kanal als Kanal sichtbar werden: neue Videos zeitnah ansehen und "
                                       "inhaltlich kommentieren; bei erkennbarer Nähe eine sachliche "
                                       "Kollaborationsanfrage über die im Kanal angegebene Kontaktmöglichkeit.",
                                       f"Der Kanal hat {channel.subscribers} Abonnenten."))

    # ---- reale Suchintentionen mit gemessenen Views
    for video_id, details in searched.items():
        for detail, (views, minutes) in sorted(details.items(), key=lambda kv: -kv[1][0]):
            if budget:
                budget.check()
            if views < MIN_SURFACE_VIEWS or len(str(detail).split()) < 2:
                continue        # Ein-Wort-Begriffe sind keine Suchintention.
            entry = learned.get("own_search_intent", {})
            if entry.get("retired"):
                continue
            store("own_search_intent", str(detail).lower(), video_id, str(detail),
                  f"https://www.youtube.com/results?search_query={str(detail).replace(' ', '+')}",
                  {"measured_views_90d": views, "measured_watch_minutes_90d": round(minutes, 1),
                   "demand_source": "own_analytics",
                   "why": (f"YouTube hat gemeldet, dass {views} Zuschauer über die Suche „{detail}“ auf dieses Video "
                           "gekommen sind – diese Nachfrage existiert nachweislich und ist noch nicht ausgeschöpft.")},
                  {"traffic_potential": score_surface(0.95, views, 0.9, 0.2, entry.get("weight", 1.0)),
                   "expected_weekly_views": expected_weekly_views(views),
                   "components": {"fit": 0.95, "present_views": views, "access": 0.9, "effort": 0.2},
                   "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit."},
                  measured_access(views, f"Den Wortlaut „{detail}“ in die ersten zwei Beschreibungszeilen und in einen "
                                 "Kapitelnamen aufnehmen, ohne Clickbait und ohne Titel/Thumbnail anzufassen."))
    # Neue Flächen aus den Suchproben: Publikum, das uns noch nicht erreicht.
    if budget:
        budget.check()
    vocab = own_vocabulary(session)
    collect_candidates(session, vocab, learned, store)
    collect_embed_sites(session, learned, store, now, http)
    pools = collect_pools(session, vocab, learned, generic_tokens(session), store,
                          video_profiles(session, generic_tokens(session)))
    session.commit()
    if owned_http is not None:
        owned_http.close()
    return {"surfaces": written, "verified": verified, "day": str(today), "per_kind": dict(per_kind),
            "pools": {"candidates": len(pools), "kept": sum(1 for p in pools if p["kept"]),
                      "verdicts": pools}}


# ----------------------------------------------------------------------------- conflicts
def metric_sources(metric):
    return METRIC_SOURCES.get(metric, ALL_SOURCES)


def blocking(session, video_id, lever_class, traffic_source, metric, exclude_id=None):
    """A running experiment blocks only what cannot be told apart from it – not the whole video.

    Blocked when the lever is the same, or when one action's traffic source lies inside the other's
    target metric. Otherwise both effects stay separately measurable and may run at the same time.
    """
    for other in session.scalars(select(GrowthAction).where(GrowthAction.video_id == video_id,
                                                            GrowthAction.status == "running")):
        if exclude_id is not None and other.id == exclude_id:
            continue
        other_lever = other.lever_class or "internal_link"
        if other_lever == (lever_class or "internal_link"):
            return other, (f"Maßnahme #{other.id} läuft am selben Hebel ({other_lever}) bis {other.evaluate_after}.")
        if traffic_source and traffic_source in metric_sources(other.target_metric):
            return other, (f"Maßnahme #{other.id} misst {other.target_metric}; darin steckt {traffic_source}. "
                           f"Beide Effekte wären nicht trennbar (bis {other.evaluate_after}).")
        if other.traffic_source and other.traffic_source in metric_sources(metric):
            return other, (f"Maßnahme #{other.id} wirkt auf {other.traffic_source}; das liegt in der neuen Zielmetrik "
                           f"{metric}. Nicht trennbar (bis {other.evaluate_after}).")
    return None, None


# ----------------------------------------------------------------------------- actions
def steps_for(kind, surface, video_title):
    access = surface.access or {}
    if kind == "own_external_referrer":
        return [f"Kontakt auf {surface.title} suchen (Impressum, Kontaktformular oder dort genannte Adresse).",
                f"Kurz und sachlich schreiben: Bezug auf die Seite nennen, „{video_title}“ als passende Ergänzung "
                "anbieten, um Verlinkung oder Erwähnung bitten – ohne Druck und ohne Gegenleistung.",
                "Nichts am Video selbst ändern. Datum und Ansprechpartner notieren.",
                NO_SPAM]
    if kind == "recommending_video":
        return [f"Das Video „{surface.title}“ wirklich ansehen: {surface.url}",
                "Einen inhaltlichen Kommentar schreiben, der auch ohne Link Wert hat (konkreter Bezug, keine Werbung).",
                "Keinen Eigenwerbe-Link setzen und nicht mehrfach kommentieren.",
                NO_SPAM]
    if kind == "curated_playlist":
        evidence = surface.evidence or {}
        return [f"Playlist ansehen: {surface.url} ({evidence.get('item_count')} Titel, Kurator „{evidence.get('owner')}“"
                + (f", {evidence.get('owner_subscribers')} Abonnenten" if evidence.get("owner_subscribers") else "")
                + ")",
                "Prüfen, ob unser Titel dort wirklich hineinpasst – Stil, Länge, Sprache. Passt es nicht, verwerfen.",
                f"Den Kanal des Kurators öffnen, Kontaktmöglichkeit suchen und kurz fragen, ob „{video_title}“ in die "
                "Playlist passt. Link mitschicken, keine Gegenleistung anbieten, nicht nachfassen.",
                "Datum und Zielstelle notieren. Nichts am Video selbst ändern.",
                NO_SPAM]
    if kind == "pool_channel":
        evidence = surface.evidence or {}
        return [f"Kanal öffnen: {surface.url} ({evidence.get('subscribers')} Abonnenten)",
                "Die neueste thematisch passende Veröffentlichung ansehen und inhaltlich kommentieren – ohne Link, "
                "ohne Eigenwerbung.",
                "Bei klarer thematischer Nähe eine sachliche Anfrage über die angegebene Kontaktmöglichkeit.",
                NO_SPAM]
    if kind == "embed_site":
        return [f"Seite öffnen, die unser Video einbettet: {surface.url}",
                "Kontakt über Impressum oder Kontaktformular suchen und den bestehenden Bezug nennen.",
                f"Sachlich anbieten, was für die Leser dort noch passt – etwa „{video_title}“ oder ein weiteres Video.",
                "Nichts am Video selbst ändern. Datum und Ansprechpartner notieren.",
                NO_SPAM]
    if kind == "candidate_video":
        return [f"Das Video ansehen: {surface.url} ({(surface.evidence or {}).get('public_views')} öffentliche Views)",
                "Einen inhaltlichen Kommentar schreiben, der auch ohne Link Wert hat – konkreter Bezug zum Video, "
                "keine Eigenwerbung, kein Link.",
                "Datum notieren. Nicht mehrfach kommentieren und nichts am eigenen Video ändern.",
                NO_SPAM]
    if kind == "candidate_channel":
        return [f"Kanal öffnen: {surface.url} ({(surface.evidence or {}).get('subscribers')} Abonnenten)",
                "Die neueste thematisch passende Veröffentlichung ansehen und inhaltlich kommentieren.",
                "Bei klarer Nähe eine sachliche Anfrage über die im Kanal angegebene Kontaktmöglichkeit – ohne "
                "Vorlagentext und ohne Gegenleistung.",
                NO_SPAM]
    if kind == "recommending_channel":
        return [f"Kanal öffnen: {surface.url} und das neueste thematisch passende Video ansehen.",
                "Dort einen inhaltlichen Kommentar als Kanal hinterlassen; bei klarer Nähe eine sachliche "
                "Kollaborationsanfrage über die angegebene Kontaktmöglichkeit.",
                "Keine Massenansprache, keine Vorlagentexte, keine Links in Kommentaren.",
                NO_SPAM]
    return [f"In der Beschreibung von „{video_title}“ die ersten zwei Zeilen auf die Suchintention "
            f"„{surface.title}“ ausrichten – im Wortlaut, ohne Clickbait.",
            "Einen Kapitelnamen ergänzen, der diese Suchintention wörtlich aufnimmt.",
            "Titel, Thumbnail und Playlist-Platzierung bleiben unverändert – das wären eigene Experimente.",
            NO_SPAM]


def mechanism(kind, surface):
    return {"own_external_referrer": ("Die Seite hat bereits Leser, die auf unser Video klicken. Eine zusätzliche oder "
                                      "aktualisierte Erwähnung erreicht genau diese Leser erneut; ihre Klicks erscheinen "
                                      "in den Analytics als EXT_URL-Views."),
            "recommending_video": ("Unter dem Video ist die Audience versammelt, die uns ohnehin empfohlen bekommt. Ein "
                                   "inhaltlich sichtbarer Kommentar führt Teile dieser Zuschauer auf unseren Kanal; "
                                   "YouTube verstärkt die Nachbarschaft, wenn Zuschauer beide Videos sehen."),
            "recommending_channel": ("Der Kanal teilt unsere Audience. Echte Teilnahme macht uns bei dessen Zuschauern "
                                     "sichtbar und erhöht die Chance, häufiger neben seinen Videos empfohlen zu werden."),
            "curated_playlist": ("Wer eine Playlist zu diesem Thema pflegt, hat Hörer, die genau solche Titel "
                                 "durchlaufen lassen. Wird unser Video aufgenommen, spielt es in dieser Rotation mit; "
                                 "die Views erscheinen in den Analytics als PLAYLIST-Quelle."),
            "pool_channel": ("Der Kanal veröffentlicht für dieselbe Zielgruppe und kennt uns nicht. Sichtbarkeit dort "
                             "führt einen Teil dieser Zuschauer zu uns; solche Klicks erscheinen als YT_OTHER_PAGE."),
            "embed_site": ("Die Seite bettet unser Video bereits ein und hat Leser, die es sehen. Eine sachliche "
                           "Ansprache kann zu einer weiteren oder besser platzierten Einbettung führen; die Views "
                           "erscheinen als EXT_URL."),
            "candidate_video": ("Unter diesem Video ist ein Publikum versammelt, das thematisch zu uns passt und uns "
                                "noch nicht kennt. Ein inhaltlich sichtbarer Kommentar führt einen Teil dieser Zuschauer "
                                "auf unseren Kanal; solche Klicks erscheinen in den Analytics als YT_OTHER_PAGE."),
            "candidate_channel": ("Der Kanal veröffentlicht laufend für genau diese Zielgruppe. Echte Teilnahme macht uns "
                                  "bei seinen Zuschauern sichtbar und kann zu Erwähnungen oder Empfehlungen führen."),
            "own_search_intent": ("Für diesen Wortlaut sucht die Zielgruppe nachweislich und findet uns schon jetzt "
                                  "gelegentlich. Wenn Beschreibung und Kapitel den Wortlaut enthalten, passt das Video "
                                  "besser zur Suchanfrage und wird für sie häufiger ausgeliefert.")}[kind]


def propose(session, now=None, budget=None):
    """One proposal per surface that is both promising and separately measurable. Nothing is executed."""
    now = now or utcnow()
    today = pacific_day(now)
    latest = session.scalar(select(func.max(TrafficSurface.day)))
    titles = {v.id: v.title for v in session.scalars(select(Video))}
    surfaces = sorted((s for s in session.scalars(select(TrafficSurface).where(TrafficSurface.day == latest))
                       if s.video_id in titles),
                      key=lambda s: -(s.scores or {}).get("traffic_potential", 0)) if latest else []
    current = {(s.kind, s.key): s for s in surfaces}
    # Zuerst pruefen, dann neu vorschlagen - auch wenn heute gar keine Flaeche uebrig bleibt.
    levers = lever_record(session)
    dropped = review_open_proposals(session, current, titles, now, levers)
    if not surfaces:
        session.commit()
        return {"proposed": 0, "blocked": [], "dropped": dropped, "day": str(latest) if latest else None}
    proposed, blocked, per_video = 0, [], defaultdict(int)
    for surface in surfaces:
        if budget:
            budget.check()
        if not (surface.access or {}).get("actionable", True):
            continue        # Beleg ja, Aufgabe nein: siehe access.why_not
        spec = SURFACE_KINDS[surface.kind]
        other, reason = blocking(session, surface.video_id, surface.lever_class, surface.traffic_source, spec["metric"])
        if other is not None:
            blocked.append({"video_id": surface.video_id, "title": titles[surface.video_id], "surface": surface.title,
                            "kind": surface.kind, "traffic_source": surface.traffic_source, "reason": reason,
                            "blocked_by": other.id, "until": str(other.evaluate_after)})
            continue
        # Genau eine offene Acquisition-Maßnahme je Video: sonst wuerde ein zweiter Lauf den
        # bestehenden Vorschlag ueberschreiben, auf den im Dashboard vielleicht schon ein Button zeigt.
        open_row = session.scalar(select(GrowthAction).where(
            GrowthAction.version == VERSION, GrowthAction.video_id == surface.video_id,
            GrowthAction.status.in_(["proposed", "running"])))
        if open_row is not None or per_video[surface.video_id] >= 1:
            continue
        payload = action_payload(surface, titles[surface.video_id], spec, levers)
        # Je Video und Tag existiert genau eine Zeile. Eine heute zurueckgezogene darf wieder aufleben,
        # sonst blockierte eine verworfene Quelle den Platz fuer die bessere bis zum naechsten Tag.
        row = session.scalar(select(GrowthAction).where(GrowthAction.version == VERSION,
                                                        GrowthAction.video_id == surface.video_id,
                                                        GrowthAction.created_day == today))
        if row is not None and row.status in ("running", "evaluated"):
            continue
        if row is None:
            row = GrowthAction(video_id=surface.video_id, created_day=today, created_at=now, version=VERSION,
                               state="traffic_acquisition", action=spec["action"], status="proposed")
            session.add(row)
        row.status, row.outcome, row.evaluation, row.evaluated_at = "proposed", None, None, None
        row.action, row.state = spec["action"], "traffic_acquisition"
        row.target_metric, row.window_days = spec["metric"], WINDOW_DAYS
        row.evaluate_after = today+timedelta(days=WINDOW_DAYS+lag_days())
        row.lever_class, row.traffic_source, row.surface_key = surface.lever_class, surface.traffic_source, surface.key
        row.payload, row.baseline = payload, payload["baseline"]
        session.flush()
        per_video[surface.video_id] += 1
        proposed += 1
    session.commit()
    return {"proposed": proposed, "blocked": blocked, "dropped": dropped, "day": str(latest)}


UPGRADE_MARGIN = 10.0        # Erst ein klar besseres Potenzial ersetzt einen offenen Vorschlag.


def review_open_proposals(session, current, titles, now, levers=None):
    """Offene Vorschläge gegen die heutige Datenlage prüfen: zurückziehen, aktualisieren oder ersetzen.

    Ohne das bliebe eine schwache Quelle für immer stehen und blockierte den Platz für eine bessere – und
    ein Vorschlag könnte im Dashboard einen Text tragen, der nicht mehr zur gemessenen Lage passt.
    Bestätigt laufende Maßnahmen bleiben unangetastet: sie werden gemessen, nicht verworfen.
    """
    dropped = []
    best_per_video = {}
    for surface in current.values():
        if not (surface.access or {}).get("actionable", True):
            continue
        spec = SURFACE_KINDS[surface.kind]
        if blocking(session, surface.video_id, surface.lever_class, surface.traffic_source, spec["metric"])[0]:
            continue
        best = best_per_video.get(surface.video_id)
        if best is None or (surface.scores or {}).get("traffic_potential", 0) > (best.scores or {}).get("traffic_potential", 0):
            best_per_video[surface.video_id] = surface
    for row in session.scalars(select(GrowthAction).where(GrowthAction.version == VERSION,
                                                          GrowthAction.status == "proposed")):
        kind = (row.payload or {}).get("surface_kind")
        surface = current.get((kind, row.surface_key))
        reason = None
        if surface is None:
            reason = "Die Fläche taucht in den heutigen Daten nicht mehr auf."
        elif not (surface.access or {}).get("actionable", True):
            reason = (surface.access or {}).get("why_not") or "Kein belastbarer Traffic-Pfad mehr."
        better = best_per_video.get(row.video_id)
        if reason is None and better is not None and better.key != row.surface_key:
            gain = (better.scores or {}).get("traffic_potential", 0)-(surface.scores or {}).get("traffic_potential", 0)
            if gain >= UPGRADE_MARGIN:
                reason = (f"Bessere Fläche gefunden: „{better.title}“ (Potenzial "
                          f"{(better.scores or {}).get('traffic_potential')} statt "
                          f"{(surface.scores or {}).get('traffic_potential')}).")
        if reason:
            row.status, row.outcome, row.evaluated_at = "superseded", "inconclusive", now
            row.evaluation = {"reason": reason, "superseded_on": str(pacific_day(now)),
                              "note": "Vorschlag, nie ausgeführt – kein Ergebnis, nur zurückgezogen."}
            dropped.append({"action_id": row.id, "surface": (row.payload or {}).get("surface_title"), "reason": reason})
        elif surface is not None:
            # Derselbe Ort, aber die Lage ist neu bewertet: Text, Zielmetrik und Baseline nachziehen.
            spec = SURFACE_KINDS[surface.kind]
            row.payload = action_payload(surface, titles.get(row.video_id, row.video_id), spec, levers)
            row.baseline = row.payload["baseline"]
            row.target_metric, row.window_days = spec["metric"], WINDOW_DAYS
            row.traffic_source, row.lever_class = surface.traffic_source, surface.lever_class
    return dropped


def action_payload(surface, video_title, spec, levers=None):
    evidence, scores = surface.evidence or {}, surface.scores or {}
    status = mechanism_status(surface, levers or {})
    return {"engine": VERSION, "surface_kind": surface.kind, "surface_key": surface.key,
            "surface_title": surface.title, "surface_url": surface.url,
            "traffic_source": surface.traffic_source, "lever_class": surface.lever_class,
            "action_label": ACTION_LABELS[spec["action"]],
            "why": evidence.get("why"), "evidence": evidence,
            "mechanism": mechanism(surface.kind, surface),
            "mechanism_status": status["status"], "mechanism_note": status["why"],
            "upgrade_rule": status.get("upgrade_rule"),
            "steps": steps_for(surface.kind, surface, video_title),
            "primary_metric": (f"zusätzliche qualifizierte Views aus {surface.traffic_source} auf dieses Video "
                               f"({spec['metric']})"),
            "target_metric": spec["metric"], "window_days": WINDOW_DAYS,
            "expected_weekly_views": scores.get("expected_weekly_views"),
            "traffic_potential": scores.get("traffic_potential"),
            "do_not_change": ["Titel", "Thumbnail", "Videoinhalt", "Sichtbarkeit"]
                             + (["Beschreibung"] if surface.kind != "own_search_intent" else []),
            "primary_lever": {"external_outreach": "eine externe Quelle ansprechen",
                              "external_community": "in einer externen Community mitwirken",
                              "playlist_placement": "Aufnahme in eine fremde Playlist erbitten",
                              "community_participation": "echte Teilnahme dort, wo die Audience ist",
                              "search_wording": "Wortlaut in Beschreibung und Kapiteln"}[surface.lever_class],
            "success_criterion": (f"Views aus {surface.traffic_source} im Nachher-Fenster messbar über dem gleich langen "
                                  "Vorher-Fenster, ohne Werbetraffic."),
            "stop_criterion": ("Keine Wiederholung und keine weitere Ansprache derselben Stelle, wenn im Messfenster "
                               "keine zusätzlichen Views aus dieser Quelle entstehen."),
            "rules": NO_SPAM, "executed_automatically": False,
            "baseline": {"note": "Wird beim Bestätigen eingefroren."},
            "note": ("Empfehlung für dich. Das System postet nichts, schreibt nichts und ändert nichts auf YouTube."),
            }


# ----------------------------------------------------------------------------- measurement
def evaluate(session, now=None):
    """Attribution per traffic source: did that surface actually send additional views?"""
    now = now or utcnow()
    today = pacific_day(now)
    known_end = today-timedelta(days=lag_days())
    evaluated = 0
    for row in session.scalars(select(GrowthAction).where(GrowthAction.version == VERSION,
                                                          GrowthAction.status == "running")):
        start = row.started_day or row.created_day
        after_end = start+timedelta(days=row.window_days)
        if after_end > known_end:
            continue
        source = row.traffic_source or "EXT_URL"
        before = source_views(session, row.video_id, source, start-timedelta(days=row.window_days),
                              start-timedelta(days=1))
        after = source_views(session, row.video_id, source, start+timedelta(days=1), after_end)
        paid = session.scalar(select(func.coalesce(func.sum(TrafficDaily.views), 0)).where(
            TrafficDaily.video_id == row.video_id, TrafficDaily.paid.is_(True),
            TrafficDaily.day >= start-timedelta(days=row.window_days), TrafficDaily.day <= after_end)) or 0
        delta = after["views"]-before["views"]
        if paid:
            outcome, detail = "inconclusive", "Werbetraffic im Messfenster; organische Attribution nicht möglich."
        elif delta > 0:
            outcome, detail = "positive", f"{before['views']} → {after['views']} Views aus {source} (+{delta})."
        elif delta < 0:
            outcome, detail = "negative", f"{before['views']} → {after['views']} Views aus {source} ({delta})."
        else:
            outcome, detail = "neutral", f"Unverändert {after['views']} Views aus {source}."
        row.status, row.outcome, row.evaluated_at = "evaluated", outcome, now
        row.evaluation = {"attribution": {"source": source, "surface_key": row.surface_key,
                                          "views_before": before["views"], "views_after": after["views"],
                                          "views_delta": delta,
                                          "watch_minutes_delta": round(after["watch_minutes"]-before["watch_minutes"], 1),
                                          "paid_views_in_window": int(paid)},
                          "before": before, "after": after, "detail": detail, "started_day": str(start),
                          "frozen_baseline": row.baseline or {},
                          "note": ("Beobachtete Veränderung der Views aus genau dieser Quelle. Saison, Algorithmus und "
                                   "andere Einflüsse sind nicht kontrolliert – kein Kausalbeweis.")}
        evaluated += 1
    session.commit()
    return evaluated


def run(session, now=None, budget=None, http=None):
    """One acquisition pass inside the existing jobs slot: measure, discover, propose."""
    now = now or utcnow()
    result = {"version": VERSION, "evaluated": 0, "surfaces": 0, "proposed": 0, "issues": []}
    for name, step in (("evaluate", lambda: {"evaluated": evaluate(session, now)}),
                       ("collect", lambda: collect(session, now, budget, http)),
                       ("propose", lambda: propose(session, now, budget))):
        try:
            result.update(step())
        except SyncBudgetExceeded:
            result["issues"].append(f"{name}: Zeitbudget erreicht; nächster Lauf setzt fort")
            break
        except Exception as exc:
            session.rollback()
            result["issues"].append(f"{name}: {type(exc).__name__}")
            log.error("Acquisition step %s failed (%s); raw data omitted", name, type(exc).__name__)
    log.info("acquisition surfaces=%s per_kind=%s proposed=%s blocked=%s evaluated=%s issues=%s pools=%s",
             result.get("surfaces"), result.get("per_kind"), result.get("proposed"),
             len(result.get("blocked") or []), result.get("evaluated"), len(result.get("issues") or []),
             {k: v for k, v in (result.get("pools") or {}).items() if k != "verdicts"})
    # Betriebssichtbarkeit fuer einen PC-off-Betrieb: was schlaegt die Engine heute konkret vor und
    # welche Flaechen stehen dahinter. Kanaleigene Daten, keine Secrets.
    try:
        view = overview(session, now)
        for entry in view["traffic_queue"]:
            log.info("acquisition proposal video=%r surface=%r url=%s source=%s metric=%s potential=%s mechanism=%s",
                     entry["title"], entry["surface"], entry.get("surface_url"), entry["traffic_source"],
                     entry["target_metric"], entry["traffic_potential"], entry.get("mechanism_status"))
        for surface in view["surfaces"][:8]:
            log.info("acquisition surface kind=%s video=%s title=%r url=%s potential=%s http=%s",
                     surface["kind"], surface["video_id"], surface["title"], surface.get("url"),
                     surface["traffic_potential"], surface["http_status"])
        for item in view["blocked"][:4]:
            log.info("acquisition blocked video=%r surface=%r source=%s reason=%r",
                     item["title"], item["surface"], item["traffic_source"], item["reason"][:120])
    except Exception as exc:
        log.info("Acquisition summary unavailable (%s)", type(exc).__name__)
    pools = result.get("pools") or {}
    log.info("acquisition pools candidates=%s kept=%s", pools.get("candidates"), pools.get("kept"))
    for verdict in (pools.get("verdicts") or [])[:12]:
        log.info("acquisition pool kind=%s kept=%s title=%r query=%r source=%s size=%s shared=%s reason=%r",
                 verdict["kind"], verdict["kept"], verdict["title"], verdict.get("query"),
                 verdict.get("query_source"), verdict.get("size"), verdict.get("shared_tokens"),
                 (verdict.get("reason") or "")[:200])
    return result


# ----------------------------------------------------------------------------- read model
def audience_report(session):
    """Welche Audience-Intents aus unseren realen Daten abgeleitet wurden – mit ihren Belegen."""
    from . import audience
    titles = {v.id: v.title for v in session.scalars(select(Video))}
    rows = []
    for intent in audience.intents(session):
        rows.append({"video_id": intent["video_id"], "video": titles.get(intent["video_id"], intent["video_id"]),
                     "label": intent["label"], "kind": intent["kind"], "query": intent["query"],
                     "head": intent["head"], "context": intent["context"],
                     "attestations": intent["attestations"], "score": intent["score"],
                     "evidence": intent["evidence"][:6], "history": intent["history"],
                     "strength": ("belegt" if intent["attestations"] >= audience.MIN_ATTESTATIONS
                                  else "einfach belegt")})
    return rows


def assessment(queue, pools, blocked, protected):
    """Ehrliche Gesamtaussage: gibt es heute eine vertretbare Aktion – oder ausdruecklich keine?

    Ein leeres JETZT TUN ist kein Fehler, sondern eine Aussage. Sie muss dastehen, damit niemand aus
    Verlegenheit eine schwache Flaeche ausprobiert.
    """
    strong = [e for e in queue if (e.get("audience_fit") or {}).get("class") in ("neighbourhood", "genre")]
    weak = [e for e in queue if e not in strong]
    if strong:
        first = strong[0]
        return {"status": "actionable", "actions": len(strong),
                "text": (f"Vertretbare Aktion vorhanden: „{first['surface']}“ für {first['title']} – "
                         f"{(first.get('audience_fit') or {}).get('why', '')}")}
    if weak:
        first = weak[0]
        return {"status": "hypothesis_only", "actions": len(weak),
                "text": ("Keine belegte musikalische Audience-Nähe gefunden. Was in der Queue steht, ist eine "
                         f"Hypothese über gemeinsames Thema: „{first['surface']}“ für {first['title']}. "
                         "Das ist ein Versuch wert, wenn du ihn als Versuch behandelst – kein belegter Kanal.")}
    reasons = [c["reason"] for c in (pools.get("candidates") or []) if c.get("kept") is False and c.get("reason")]
    return {"status": "none", "actions": 0,
            "text": ("Aktuell keine ausreichend gute Traffic-Aktion. "
                     + (f"Häufigster Grund bei {len(reasons)} geprüften Kandidaten: „{reasons[0][:180]}“ " if reasons
                        else "")
                     + (f"{len(blocked)} Fläche(n) sind wegen eines laufenden Experiments nicht trennbar. "
                        if blocked else "")
                     + (f"{len(protected)} bestehende Quelle(n) bleiben geschützt und werden nur gemessen."
                        if protected else "").strip())}


def pool_report(session, active_queries=()):
    """Was die Pool-Suche wirklich gefunden hat und was damit passiert ist - ohne Beschoenigung."""
    probe, run = {}, session.scalar(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()))
    if run is not None:
        probe = (run.stats or {}).get("pool_probe") or {}
    candidates = []
    for pool in session.scalars(select(AudiencePool).order_by(AudiencePool.last_seen_day.desc(),
                                                             AudiencePool.id.desc()).limit(24)):
        details = pool.details or {}
        verdict = details.get("verdict") or {}
        candidates.append({"kind": pool.kind, "title": pool.title, "url": pool.url, "query": pool.query,
                           "query_source": details.get("query_source"), "size": pool_size(pool),
                           "intent": (details.get("intent") or {}).get("label"),
                           "stale": bool(active_queries) and pool.query not in active_queries,
                           "found_day": str(pool.first_seen_day) if pool.first_seen_day else None,
                           "kept": verdict.get("kept"), "reason": verdict.get("reason"),
                           "fit_class": verdict.get("fit_class"),
                           "shared_tokens": verdict.get("shared_tokens") or []})
    return {"searched": bool(probe.get("queries")), "note": probe.get("note"),
            "queries": probe.get("queries") or [], "available_queries": probe.get("available_queries") or [],
            "day": str(run.day) if run is not None else None,
            "run_status": run.status if run is not None else None,
            "candidates": candidates, "kept": sum(1 for c in candidates if c["kept"]),
            "rejected": sum(1 for c in candidates if c["kept"] is False)}


def overview(session, now=None):
    """The traffic queue: only executable acquisition actions, ranked by traffic potential."""
    now = now or utcnow()
    today = pacific_day(now)
    titles = {v.id: v.title for v in session.scalars(select(Video))}
    latest = session.scalar(select(func.max(TrafficSurface.day)))
    surfaces = list(session.scalars(select(TrafficSurface).where(TrafficSurface.day == latest))) if latest else []
    rows = list(session.scalars(select(GrowthAction).where(GrowthAction.version == VERSION)))
    by_key = {(s.kind, s.key): s for s in surfaces}
    queue, running, results = [], [], []
    for row in sorted(rows, key=lambda r: -((r.payload or {}).get("traffic_potential") or 0)):
        payload = row.payload or {}
        surface = by_key.get((payload.get("surface_kind"), row.surface_key))
        entry = {"action_id": row.id, "status": row.status, "video_id": row.video_id,
                 "title": titles.get(row.video_id, row.video_id), "action": row.action,
                 "action_label": payload.get("action_label"), "traffic_source": row.traffic_source,
                 "lever_class": row.lever_class, "surface": payload.get("surface_title"),
                 "surface_url": payload.get("surface_url"), "surface_kind": payload.get("surface_kind"),
                 "why": payload.get("why"), "mechanism": payload.get("mechanism"), "steps": payload.get("steps") or [],
                 "audience_fit": (payload.get("evidence") or {}).get("audience_fit"),
                 "mechanism_status": payload.get("mechanism_status"), "mechanism_note": payload.get("mechanism_note"),
                 "upgrade_rule": payload.get("upgrade_rule"), "activity": (payload.get("evidence") or {}).get("activity"),
                 "primary_metric": payload.get("primary_metric"), "target_metric": row.target_metric,
                 "window_days": row.window_days, "evaluate_after": str(row.evaluate_after),
                 "do_not_change": payload.get("do_not_change") or [], "primary_lever": payload.get("primary_lever"),
                 "success_criterion": payload.get("success_criterion"), "stop_criterion": payload.get("stop_criterion"),
                 "expected_weekly_views": payload.get("expected_weekly_views"),
                 "traffic_potential": payload.get("traffic_potential"),
                 "evidence": payload.get("evidence") or {}, "rules": payload.get("rules"),
                 "verified": bool(surface and surface.http_status and surface.http_status < 400) if surface else None,
                 "executed_automatically": False,
                 "confirm": {"required": True, "label": "Als durchgeführt markieren – Messfenster starten",
                             "endpoint": f"/api/growth/actions/{row.id}/start",
                             "effect": "Friert die Baseline dieser Trafficquelle ein und startet das Messfenster."},
                 "note": payload.get("note")}
        if row.status == "proposed":
            queue.append(entry)
        elif row.status == "running":
            running.append({**entry, "started_day": str(row.started_day) if row.started_day else None,
                            "baseline": row.baseline or {}})
        elif row.status == "evaluated":
            results.append({**entry, "outcome": row.outcome,
                            "attribution": (row.evaluation or {}).get("attribution"),
                            "detail": (row.evaluation or {}).get("detail")})
    for rank, entry in enumerate(queue[:QUEUE_LIMIT], 1):
        entry["rank"] = rank
    intents_view = audience_report(session)
    blocked = []
    for surface in sorted(surfaces, key=lambda s: -(s.scores or {}).get("traffic_potential", 0)):
        if not (surface.access or {}).get("actionable", True):
            continue        # Kein Traffic-Pfad: das ist kein Konflikt, sondern fehlende Evidenz.
        spec = SURFACE_KINDS[surface.kind]
        other, reason = blocking(session, surface.video_id, surface.lever_class, surface.traffic_source, spec["metric"])
        if other is not None:
            blocked.append({"video_id": surface.video_id, "title": titles.get(surface.video_id), "kind": surface.kind,
                            "surface": surface.title, "traffic_source": surface.traffic_source, "reason": reason,
                            "blocked_by": other.id, "until": str(other.evaluate_after)})
    protected = [{"title": s.title, "url": s.url, "kind": s.kind, "traffic_source": s.traffic_source,
                  "video": titles.get(s.video_id, s.video_id),
                  "measured_views_90d": (s.evidence or {}).get("measured_views_90d"),
                  "why_not": (s.access or {}).get("why_not")}
                 for s in sorted(surfaces, key=lambda s: -((s.evidence or {}).get("measured_views_90d") or 0))
                 if (s.access or {}).get("protected")]
    pools_view = pool_report(session, {row["query"] for row in intents_view})
    return {"version": VERSION, "day": str(latest) if latest else None, "today": str(today),
            "protected_sources": protected[:12],
            "assessment": assessment(queue[:QUEUE_LIMIT], pools_view, blocked, protected),
            "traffic_queue": queue[:QUEUE_LIMIT], "running": running, "results": results[:8],
            "blocked": blocked[:8], "surfaces_found": len(surfaces),
            "surfaces": [{"kind": s.kind, "key": s.key, "title": s.title, "url": s.url, "video_id": s.video_id,
                          "traffic_source": s.traffic_source, "http_status": s.http_status,
                          "traffic_potential": (s.scores or {}).get("traffic_potential"),
                          "expected_weekly_views": (s.scores or {}).get("expected_weekly_views"),
                          "why": (s.evidence or {}).get("why")}
                         for s in sorted(surfaces, key=lambda s: -(s.scores or {}).get("traffic_potential", 0))[:12]],
            "scoreboard": scoreboard(session), "learning": weights(session), "levers": lever_record(session),
            "pools": pools_view, "audience_intents": intents_view,
            "capabilities": {"used": ["Eigene Analytics: externe Referrer (EXT_URL-Detail)",
                                      "Eigene Analytics: empfehlende Videos und deren Kanäle",
                                      "Eigene Analytics: reale Suchbegriffe",
                                      "Öffentliche Suchproben (YouTube Data API) für thematisch nahe Videos/Kanäle",
                                      "Öffentliche YouTube-Suche nach fremden Playlists und Kanälen (search.list)",
                                      "Eigene Analytics: Seiten, die unser Video einbetten",
                                      "HTTP-Prüfung, dass eine Seite erreichbar ist"],
                             "not_used": ["Automatisches Posten, Kommentieren oder Anschreiben: findet nicht statt.",
                                          "Bezahlte Reichweite, Bots, Engagement-Pods: ausgeschlossen.",
                                          "Externe Web-Suchanbieter: verworfen, keine zusätzlichen Kosten.",
                                          "Bestehende Radios, Redaktionen, Medien, Kuratoren und einbettende "
                                          "Seiten: werden gemessen, aber nie angesprochen."]},
            "read_only": "Das System postet nichts und ändert nichts auf YouTube.",
            "goal": "Zusätzliche qualifizierte organische Views; gemessen wird die Quelle, nicht die Aktivität."}
