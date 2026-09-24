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
from .models import (ChannelPlaylist, DiscoveryChannel, DiscoveryItem, DiscoverySignal, GrowthAction, TrafficDaily,
                     TrafficSurface, Video, utcnow)

log = logging.getLogger(__name__)
VERSION = "acquisition-v7"
SIGNAL_WINDOW_DAYS = 90
MIN_SURFACE_VIEWS = 1        # Sichtbar wird eine Fläche ab einem gemessenen View von dort.
MIN_ACTIONABLE_VIEWS = 5     # Vorgeschlagen wird erst, wenn dort in 90 Tagen wirklich Publikum war.
MIN_CANDIDATE_VIEWS = 2000   # Öffentliche Reichweite, ab der ein fremdes Video als Fläche zählt.
MIN_CANDIDATE_SUBSCRIBERS = 500
MIN_SHARED_TOKENS = 2        # Ein gemeinsames Wort ist keine Themengleichheit (siehe V6).
# Formatwörter beschreiben die Verpackung, nicht das Thema: „Album Teaser“ passt auf jedes Album-Teaser
# der Welt. Der erste Lauf hat darüber ein BLACKPINK-Video als Fläche für unser Teaser-Video vorgeschlagen.
FORMAT_WORDS = {"album", "teaser", "single", "trailer", "visual", "preview", "snippet", "clip", "extended",
                "instrumental", "karaoke", "cover", "medley", "bonus", "deluxe", "release", "promo", "playlist",
                "compilation", "collection", "megamix", "session", "sessions", "acoustic", "unplugged"}
GENERIC_SHARE = 0.05         # Ein Begriff in mehr als 5 % aller fremden Titel ist ein Formatwort, kein Thema.
GENERIC_MIN_CORPUS = 50
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
ALL_SOURCES = DISCOVERY_SOURCES | {"EXT_URL", "SHORTS", "ADVERTISING", "NO_LINK_OTHER", "NO_LINK_EMBEDDED"}
# impressions_7d zaehlt nur Auslieferungen auf YouTube-Oberflaechen: ein externer Link erzeugt Views,
# aber keine Thumbnail-Impression. Deshalb ist externe Ansprache davon trennbar.
METRIC_SOURCES = {"discovery_views_7d": DISCOVERY_SOURCES, "impressions_7d": DISCOVERY_SOURCES,
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
}
# Plattformen, über die Menschen Links privat weiterschicken. Sie sind Beleg dafür, dass geteilt wird,
# aber keine Stelle, die man ansprechen kann – „whatsapp.com kontaktieren“ wäre ein Phantom-Auftrag.
SHARE_PLATFORMS = {"whatsapp.com", "t.co", "twitter.com", "x.com", "facebook.com", "l.facebook.com", "m.facebook.com",
                   "instagram.com", "l.instagram.com", "telegram.org", "web.telegram.org", "google.com",
                   "news.google.com", "youtube.com", "m.youtube.com", "youtu.be", "linkedin.com", "pinterest.com",
                   "tiktok.com", "snapchat.com", "discord.com", "mail.google.com", "outlook.com", "bing.com",
                   "duckduckgo.com", "yandex.ru", "baidu.com", "messenger.com", "reddit.com", "linktr.ee"}
ACTION_LABELS = {"reach_out_to_referrer": "Externe Quelle ansprechen, die schon Zuschauer schickt",
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


def measured_access(views, how):
    """Gemessene Flächen brauchen einen belastbaren Traffic-Pfad, sonst bleiben sie Beleg statt Aufgabe."""
    if views < MIN_ACTIONABLE_VIEWS:
        return {"actionable": False, "rules": NO_SPAM, "manual": True,
                "why_not": (f"Nur {views} Views in 90 Tagen von dieser Fläche: zu wenig für einen belastbaren "
                            f"Traffic-Pfad. Ab {MIN_ACTIONABLE_VIEWS} Views wird daraus eine Aufgabe.")}
    return {"actionable": True, "rules": NO_SPAM, "manual": True, "how": how}


def referrer_access(url, views):
    """Kann man dort überhaupt etwas tun – und lohnt es sich nach gemessenem Publikum?"""
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
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
    return {video_id: set(list(words)[:MAX_VOCAB_TOKENS*2]) for video_id, words in vocab.items()}


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


def candidate_matches(session, vocab, generic=None):
    """Fremde Videos aus den Suchproben, die thematisch mehrfach zu einem unserer Videos passen."""
    from .discovery import tokens as split, BRAND
    generic = generic if generic is not None else generic_tokens(session)
    ignore = BRAND | generic
    matches = []
    for item in session.scalars(select(DiscoveryItem)):
        if not (item.via or {}).get("queries") or item.video_id in vocab:
            continue
        words = set(split(item.title))|{t for tag in (item.tags or []) for t in split(tag)}
        best, shared = None, []
        for video_id, own in vocab.items():
            common = sorted((words & own)-ignore)
            if len(common) > len(shared):
                best, shared = video_id, common
        if best is not None and len(shared) >= MIN_SHARED_TOKENS:
            matches.append((item, best, tuple(shared)))
    return matches


def collect_candidates(session, vocab, learned, store):
    """Neue Flächen, mehrfach bestätigt: mindestens zwei gemeinsame Begriffe und zwei verschiedene Kanäle.

    Die Reichweite ist öffentlich nachprüfbar (Views, Abonnenten); dass dieses Publikum uns erreicht, ist
    ausdrücklich nicht gemessen. Genau deshalb der Abschlag in der Bewertung und der Proxy-Hinweis.
    """
    generic = generic_tokens(session)
    matches = candidate_matches(session, vocab, generic)
    channels = {row.channel_id: row for row in session.scalars(select(DiscoveryChannel))}
    seen_channels = set()
    for item, video_id, shared in sorted(matches, key=lambda m: -(m[0].views or 0)):
        # Korroboration auf Themenebene: andere Treffer desselben Videos, die mindestens zwei Begriffe
        # mit diesem teilen. Identische Wortmengen zu verlangen waere zu streng – Titel sind verschieden.
        related = [(other, other_channel) for other, other_video, other_shared in matches
                   for other_channel in [other.channel_id]
                   if other_video == video_id and len(set(other_shared) & set(shared)) >= MIN_SHARED_TOKENS]
        members = len(related)
        distinct = len({channel for _, channel in related if channel})
        if members < 2 or distinct < 2:
            continue        # Ein einzelner Treffer ist Zufall, nicht ein Thema.
        channel = channels.get(item.channel_id)
        subscribers = channel.subscribers if channel else None
        corroboration = (f"{members} Videos aus {distinct} verschiedenen Kanälen teilen die Begriffe "
                         f"{', '.join(shared)} mit unserem Video.")
        if (item.views or 0) >= MIN_CANDIDATE_VIEWS and not learned.get("candidate_video", {}).get("retired"):
            store("candidate_video", item.video_id, video_id, item.title,
                  f"https://www.youtube.com/watch?v={item.video_id}",
                  {"public_views": item.views, "channel": item.channel_title, "subscribers": subscribers,
                   "shared_tokens": list(shared), "members": members, "channels": distinct,
                   "demand_source": "public_proxy_corroborated", "found_via": (item.via or {}).get("queries", [])[:3],
                   "generic_words_ignored": sorted(generic & set(shared)) or None,
                   "why": (f"Dieses Video hat {item.views} öffentlich gezählte Views und liegt thematisch neben uns: "
                           f"{corroboration} Sein Publikum ist belegt vorhanden – es erreicht uns nur noch nicht."),
                   "uncertainty": "mittel: Reichweite öffentlich belegt, eigener Zufluss noch nicht gemessen."},
                  {"traffic_potential": score_candidate(shared, item.views, subscribers,
                                                        learned.get("candidate_video", {}).get("weight", 1.0)),
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
                   "why": (f"„{channel.title}“ hat {subscribers} Abonnenten und veröffentlicht thematisch nahe Videos: "
                           f"{corroboration} Dort ist ein Publikum, das zu unserem Video passt."),
                   "uncertainty": "mittel: Kanalgröße öffentlich belegt, eigener Zufluss noch nicht gemessen."},
                  {"traffic_potential": score_candidate(shared, channel.views, subscribers,
                                                        learned.get("candidate_channel", {}).get("weight", 1.0)),
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
                      {"traffic_potential": score_surface(0.9, views, 0.6, 0.4, entry.get("weight", 1.0)),
                       "expected_weekly_views": expected_weekly_views(views),
                       "components": {"fit": 0.9, "present_views": views, "access": 0.6, "effort": 0.4},
                       "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit."},
                      measured_access(views, "Unter diesem Video als Kanal echt teilnehmen: das Video ansehen und "
                                     "einen inhaltlichen Kommentar schreiben, der ohne Link Wert hat. Kein "
                                     "Eigenwerbe-Link, keine Wiederholung."))
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
                      {"traffic_potential": score_surface(0.85, views, 0.5, 0.5, kind_entry.get("weight", 1.0)),
                       "expected_weekly_views": expected_weekly_views(views),
                       "components": {"fit": 0.85, "present_views": views, "access": 0.5, "effort": 0.5},
                       "note": "Relativer Wert für diesen Kanal, keine Wahrscheinlichkeit."},
                      measured_access(views, "Beim Kanal als Kanal sichtbar werden: neue Videos zeitnah ansehen und "
                                     "inhaltlich kommentieren; bei erkennbarer Nähe eine sachliche "
                                     "Kollaborationsanfrage über die im Kanal angegebene Kontaktmöglichkeit."))

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
    collect_candidates(session, own_vocabulary(session), learned, store)
    session.commit()
    if owned_http is not None:
        owned_http.close()
    return {"surfaces": written, "verified": verified, "day": str(today), "per_kind": dict(per_kind)}


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
    if latest is None:
        return {"proposed": 0, "blocked": [], "day": None}
    titles = {v.id: v.title for v in session.scalars(select(Video))}
    surfaces = sorted((s for s in session.scalars(select(TrafficSurface).where(TrafficSurface.day == latest))
                       if s.video_id in titles),
                      key=lambda s: -(s.scores or {}).get("traffic_potential", 0))
    current = {(s.kind, s.key): s for s in surfaces}
    dropped = retire_weak_proposals(session, current, now)
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
        payload = action_payload(surface, titles[surface.video_id], spec)
        row = session.scalar(select(GrowthAction).where(GrowthAction.version == VERSION,
                                                        GrowthAction.video_id == surface.video_id,
                                                        GrowthAction.created_day == today))
        if row is not None and row.status not in ("proposed",):
            continue
        if row is None:
            row = GrowthAction(video_id=surface.video_id, created_day=today, created_at=now, version=VERSION,
                               state="traffic_acquisition", action=spec["action"], status="proposed")
            session.add(row)
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


def retire_weak_proposals(session, current, now):
    """Ein Vorschlag, dessen Fläche keinen belastbaren Traffic-Pfad mehr hat, wird zurückgezogen.

    Ohne das bliebe eine schwache Quelle für immer oben stehen und blockierte den Platz für eine
    bessere. Bestätigt laufende Maßnahmen bleiben unangetastet – sie werden gemessen, nicht verworfen.
    """
    dropped = []
    for row in session.scalars(select(GrowthAction).where(GrowthAction.version == VERSION,
                                                          GrowthAction.status == "proposed")):
        kind = (row.payload or {}).get("surface_kind")
        surface = current.get((kind, row.surface_key))
        reason = None
        if surface is None:
            reason = "Die Fläche taucht in den heutigen Daten nicht mehr auf."
        elif not (surface.access or {}).get("actionable", True):
            reason = (surface.access or {}).get("why_not") or "Kein belastbarer Traffic-Pfad mehr."
        if reason:
            row.status, row.outcome, row.evaluated_at = "superseded", "inconclusive", now
            row.evaluation = {"reason": reason, "superseded_on": str(pacific_day(now)),
                              "note": "Vorschlag, nie ausgeführt – kein Ergebnis, nur zurückgezogen."}
            dropped.append({"action_id": row.id, "surface": (row.payload or {}).get("surface_title"), "reason": reason})
    return dropped


def action_payload(surface, video_title, spec):
    evidence, scores = surface.evidence or {}, surface.scores or {}
    return {"engine": VERSION, "surface_kind": surface.kind, "surface_key": surface.key,
            "surface_title": surface.title, "surface_url": surface.url,
            "traffic_source": surface.traffic_source, "lever_class": surface.lever_class,
            "action_label": ACTION_LABELS[spec["action"]],
            "why": evidence.get("why"), "evidence": evidence,
            "mechanism": mechanism(surface.kind, surface),
            "steps": steps_for(surface.kind, surface, video_title),
            "primary_metric": (f"zusätzliche qualifizierte Views aus {surface.traffic_source} auf dieses Video "
                               f"({spec['metric']})"),
            "target_metric": spec["metric"], "window_days": WINDOW_DAYS,
            "expected_weekly_views": scores.get("expected_weekly_views"),
            "traffic_potential": scores.get("traffic_potential"),
            "do_not_change": ["Titel", "Thumbnail", "Videoinhalt", "Sichtbarkeit"]
                             + (["Beschreibung"] if surface.kind != "own_search_intent" else []),
            "primary_lever": {"external_outreach": "eine externe Quelle ansprechen",
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
    log.info("acquisition surfaces=%s per_kind=%s proposed=%s blocked=%s evaluated=%s issues=%s",
             result.get("surfaces"), result.get("per_kind"), result.get("proposed"),
             len(result.get("blocked") or []), result.get("evaluated"), len(result.get("issues") or []))
    # Betriebssichtbarkeit fuer einen PC-off-Betrieb: was schlaegt die Engine heute konkret vor und
    # welche Flaechen stehen dahinter. Kanaleigene Daten, keine Secrets.
    try:
        view = overview(session, now)
        for entry in view["traffic_queue"]:
            log.info("acquisition proposal video=%r surface=%r source=%s metric=%s potential=%s expected_weekly=%s",
                     entry["title"], entry["surface"], entry["traffic_source"], entry["target_metric"],
                     entry["traffic_potential"], entry["expected_weekly_views"])
        for surface in view["surfaces"][:6]:
            log.info("acquisition surface kind=%s video=%s title=%r potential=%s expected_weekly=%s http=%s",
                     surface["kind"], surface["video_id"], surface["title"], surface["traffic_potential"],
                     surface["expected_weekly_views"], surface["http_status"])
        for item in view["blocked"][:4]:
            log.info("acquisition blocked video=%r surface=%r source=%s reason=%r",
                     item["title"], item["surface"], item["traffic_source"], item["reason"][:120])
    except Exception as exc:
        log.info("Acquisition summary unavailable (%s)", type(exc).__name__)
    return result


# ----------------------------------------------------------------------------- read model
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
    return {"version": VERSION, "day": str(latest) if latest else None, "today": str(today),
            "traffic_queue": queue[:QUEUE_LIMIT], "running": running, "results": results[:8],
            "blocked": blocked[:8], "surfaces_found": len(surfaces),
            "surfaces": [{"kind": s.kind, "key": s.key, "title": s.title, "url": s.url, "video_id": s.video_id,
                          "traffic_source": s.traffic_source, "http_status": s.http_status,
                          "traffic_potential": (s.scores or {}).get("traffic_potential"),
                          "expected_weekly_views": (s.scores or {}).get("expected_weekly_views"),
                          "why": (s.evidence or {}).get("why")}
                         for s in sorted(surfaces, key=lambda s: -(s.scores or {}).get("traffic_potential", 0))[:12]],
            "scoreboard": scoreboard(session), "learning": weights(session),
            "capabilities": {"used": ["Eigene Analytics: externe Referrer (EXT_URL-Detail)",
                                      "Eigene Analytics: empfehlende Videos und deren Kanäle",
                                      "Eigene Analytics: reale Suchbegriffe",
                                      "HTTP-Prüfung, dass eine externe Seite erreichbar ist"],
                             "not_used": ["Web-/Foren-/Blog-Suche nach neuen Communities: benötigt einen Such-Provider "
                                          "mit API-Key, in diesem Sprint ausgeschlossen. Es werden deshalb nur Flächen "
                                          "vorgeschlagen, die den Kanal nachweislich schon berühren."]},
            "read_only": "Das System postet nichts und ändert nichts auf YouTube.",
            "goal": "Zusätzliche qualifizierte organische Views; gemessen wird die Quelle, nicht die Aktivität."}
