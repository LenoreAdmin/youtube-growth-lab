"""V6 External Audience Discovery: find where real, relevant viewers already are – read-only, budgeted, honest.

Sources that actually exist with this stack:
- own analytics `insightTrafficSourceDetail` (search terms, recommending videos, external URLs) – real demand,
- Data API `search.list` probes (100 units each) and `videos/channels.list` metadata (1 unit) – public proxies.
Nothing else is automated (no Trends, no autocomplete, no scraping). Search volumes do not exist here; every
non-analytics demand figure is labelled a proxy. Scores are relative priorities, never probabilities, and
public data never raises n_videos. Paid traffic never counts as organic demand evidence.
"""
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import log10, tanh
from statistics import median
import isodate
from sqlalchemy import select, func
from googleapiclient.errors import HttpError
from .db import Session
from .models import (Video, TrafficDaily, AudiencePool, ChannelPlaylist, DiscoveryRun, DiscoveryQuota, DiscoveryQuery,
                     DiscoveryItem, DiscoveryChannel, DiscoverySignal, DiscoveryOpportunity, LearningDataset,
                     VideoProfile, utcnow)
from .backfill import upsert, classify as classify_error
from .budget import Budget, SyncBudgetExceeded
from .jobs import acquire, release
from .history import load as load_histories, features_at, paid_profile, pacific_day, lag_days
from .metrics import aware
from .config import settings

log = logging.getLogger(__name__)
VERSION = "discovery-v6"
LEASE = "youtube-discovery"
DAILY_UNITS = 1500          # Data API units per Pacific day reserved for discovery (project default quota: 10,000).
SEARCH_COST, LIST_COST = 100, 1
MAX_SEARCHES_PER_RUN = 8
REPROBE_DAYS, FAIL_BACKOFF_DAYS, CHANNEL_REFRESH_DAYS = 7, 3, 30
SIGNAL_WINDOW_DAYS = 90
MEMORY_DAYS = 28
MIN_SEED_TOKENS = 2        # Single generic words ("pop", "note") are not search intents.
MIN_RELEVANCE = 0.34       # Seed relevance is a filter only, never positive evidence.
PROXY_SCORE_CAP = 45       # A single proxy family must not look like validated demand.
MULTI_PROXY_SCORE_CAP = 70  # Several independent families may justify an experiment, but stay below own analytics.
MIN_PROXY_FAMILIES = 2     # One proxy is never enough; two independent families may carry a hypothesis.
RETIRED_PRIORITY = -1.0    # Generic legacy seeds keep their row for audit but are never probed again.
EVIDENCE_LEVELS = ("own_analytics", "multi_signal_proxy", "weak_proxy", "none")
ACTIONABLE_LEVELS = ("own_analytics", "multi_signal_proxy")
LEVEL_CAPS = {"own_analytics": 100.0, "multi_signal_proxy": MULTI_PROXY_SCORE_CAP, "weak_proxy": PROXY_SCORE_CAP}
# Unabhängige Evidenzfamilien. Die Herkunft bleibt sichtbar; ein aus dem eigenen Titel gewonnener Seed
# ist zirkulär und ist niemals selbst eine Familie, wie gut er auch passt.
# Familien sind Datenrouten, nicht Metriken: alles, was aus derselben Suchprobe abgeleitet ist
# (Ergebnismenge, Median-Views, Wettbewerb, eigener Rang), bleibt EINE Familie. Sonst würde ein
# einzelner Proxy durch Umbenennen seiner Kennzahlen wie mehrfache Evidenz aussehen.
FAMILIES = {
    "own_term_demand": "Eigene Analytics: Views aus genau diesem Suchbegriff bzw. über dieses empfehlende Video (Detailebene)",
    "own_traffic_mix": "Eigene Analytics: reale organische Views aus dieser Traffic-Quelle in 90 Tagen (aggregiert, ohne Begriffsdetail)",
    "search_probe": "Öffentliche Suchprobe (search.list): Ergebnismenge, Wettbewerb, eigener Rang – eine Beobachtung",
    "neighbour_metadata": "Öffentliche Metadaten eines Nachbarvideos, das über die eigene Empfehlungsquelle entdeckt wurde",
    "cluster_corroboration": "Mehrere Nachbarvideos verschiedener Kanäle, über mehrere Routen entdeckt",
}
OWN_FAMILIES = ("own_term_demand", "own_traffic_mix")
# Bevor eine fremde Chance den Wortlaut für Beschreibung oder Playlist vorgeben darf, muss das Thema
# belegt sein. Ein einzelnes gemeinsames Wort ("shine") ist Wortgleichheit, nicht Themengleichheit:
# "Shine On" und "Shine Jesus Shine" teilen ein Token und sonst nichts.
MIN_CONTEXT_TOKENS = 2      # mindestens zwei gemeinsame, nicht generische Begriffe
MIN_CONTEXT_MEMBERS = 3     # oder Bestätigung durch mehrere unabhängige Nachbarvideos
MIN_CONTEXT_CHANNELS = 2    # aus mindestens zwei verschiedenen Kanälen
MIN_CONTEXT_RELEVANCE = 0.5
SOURCE_FOR_KIND = {"search": "YT_SEARCH", "suggested": "RELATED_VIDEO", "cluster": "RELATED_VIDEO"}
SOURCE_KINDS = {"YT_SEARCH": "own_search_term", "RELATED_VIDEO": "own_suggested_source", "EXT_URL": "own_external"}
MAX_POOL_SEARCHES = 4        # Je Lauf bis zu vier Suchen: 400 Einheiten von 1500.
MAX_POOL_ITEMS = 8           # So viele Treffer je Suche werden mit Detailabfragen angereichert.
MAX_POOL_QUERIES = 4         # Kandidaten-Queries; verwendet werden die besten zwei.
POOL_RESERVE = MAX_POOL_SEARCHES*SEARCH_COST+20*LIST_COST   # Reserve fuer die Pool-Suche (420).
POOL_RESERVE_SHARE = 0.3    # Bei kleinem Tagesbudget wird entsprechend weniger reserviert.
GAPS = ["existing_video_opportunity", "packaging_opportunity", "search_opportunity", "suggested_opportunity",
        "followup_content_opportunity", "insufficient_evidence"]
BRAND = {"sealand", "sealandmusic"}
STOPWORDS = BRAND | {"official", "video", "music", "musik", "feat", "ft", "the", "and", "und", "der", "die", "das", "ein", "eine", "mit",
    "von", "for", "with", "you", "your", "our", "new", "neu", "hd", "4k", "full", "mix", "remix", "audio", "lyrics", "lyric", "song",
    "songs", "version", "edit", "live", "vs", "aus", "auf", "für", "im", "in", "zu", "am", "an", "of", "to", "on", "at", "by", "is",
    "are", "was", "der", "den", "dem", "des", "es", "ist", "top", "best", "beste", "hours", "hour", "stunden", "min", "part", "teil"}
SCORE_NOTE = ("Relativer Priorisierungswert (0–100) aus verfügbaren Komponenten; keine Wahrscheinlichkeit, kein Suchvolumen. "
              f"Ohne eigene Analytics-Nachfrage bei {PROXY_SCORE_CAP} gedeckelt; die Seed-Relevanz ist zirkulär und wird nicht gewertet.")
CAPABILITIES = [
    {"source": "Eigene Analytics: insightTrafficSourceDetail (Suchbegriffe, empfehlende Videos, externe URLs)", "status": "genutzt",
     "note": "Reale Nachfrage-Evidenz für eigene Videos; bestehender Analytics-Scope, keine Data-API-Quota; max. 25 Zeilen je Abfrage."},
    {"source": "YouTube Data API search.list", "status": "genutzt (budgetiert)", "note": f"100 Einheiten je Suche; Tagesbudget {DAILY_UNITS} Einheiten, max. {MAX_SEARCHES_PER_RUN} Suchen je Lauf, Cache {REPROBE_DAYS} Tage. Liefert Relevanz-Ranking, kein Suchvolumen (Proxy)."},
    {"source": "YouTube Data API videos.list / channels.list (öffentliche Metadaten)", "status": "genutzt", "note": "1 Einheit je 50 IDs: Titel, Tags, Views, Alter, Abonnenten von Nachbarvideos/-kanälen."},
    {"source": "relatedToVideoId", "status": "nicht verfügbar", "note": "Von Google 2023 entfernt; ersetzt durch eigene RELATED_VIDEO-Quellen und Such-Proben."},
    {"source": "Google Trends", "status": "nicht genutzt", "note": "Keine offizielle API; inoffizielle Bibliotheken scrapen und werden blockiert – nicht zuverlässig automatisierbar."},
    {"source": "YouTube-Autocomplete, Reddit, Spotify, Last.fm", "status": "nicht genutzt", "note": "Undokumentiert bzw. neue Registrierung/Keys nötig."},
    {"source": "Echtes Suchvolumen", "status": "nicht verfügbar", "note": "Alle Nachfragewerte außer eigenen Analytics-Views sind Proxies und so gekennzeichnet."},
]


class Throttled(Exception):
    """Data API quota or upstream limit; stop the run, keep progress, retry on a later run."""


class QuotaExhausted(Exception):
    """Own daily discovery budget reached; remaining work resumes on the next day."""


# ----------------------------------------------------------------------------- text helpers
# „K-Pop“, „J-Rock“, „G-Funk“ sind eigene Genres. Ohne diese Zeile zerfaellt „K-pop“ in „pop“ und
# ein K-Pop-Tanzworkshop teilt plötzlich unser Genre.
COMPOUND_GENRES = ("pop", "rock", "rap", "jazz", "punk", "funk", "soul", "wave", "metal", "house", "step")
LETTERS = "abcdefghijklmnopqrstuvwxyz"


def join_compound_genres(text):
    """„K-Pop“, „J-Rock“, „G-Funk“ sind eigene Genres.

    Ohne diesen Schritt zerfaellt „K-pop“ beim Zerlegen in „pop“, und ein K-Pop-Tanzworkshop teilt
    ploetzlich unser Genre. Ein Bindestrich zwischen einem einzelnen Buchstaben und einem Genrenamen
    gehoert zum Namen.
    """
    for genre in COMPOUND_GENRES:
        if genre not in text:
            continue
        for letter in LETTERS:
            text = text.replace(f"{letter}-{genre}", f"{letter}{genre}")
    return text


def tokens(text, keep_brand=False):
    words = re.findall(r"[a-z0-9äöüß]+", join_compound_genres((text or "").lower()))
    return [w for w in words if len(w) >= 3 and (w not in STOPWORDS or (keep_brand and w in BRAND))]


def normalize(query):
    return " ".join(tokens(query, keep_brand=True))[:200]


def overlap(query_tokens, vocabulary):
    if not query_tokens:
        return 0.0
    return sum(t in vocabulary for t in query_tokens)/len(query_tokens)


# ----------------------------------------------------------------------------- quota
class Quota:
    def __init__(self, session, day, limit=None):
        self.session, self.day, self.limit = session, day, DAILY_UNITS if limit is None else limit
        row = session.get(DiscoveryQuota, day)
        self.used = row.units if row else 0
        self.spent_now = 0

    def remaining(self):
        return self.limit-self.used

    def spend(self, units):
        if self.used+units > self.limit:
            raise QuotaExhausted()
        self.used += units
        self.spent_now += units
        statement = upsert(self.session, DiscoveryQuota).values(day=self.day, units=self.used, updated_at=utcnow())
        self.session.execute(statement.on_conflict_do_update(index_elements=["day"], set_={"units": statement.excluded.units, "updated_at": statement.excluded.updated_at}))


def analytics_end(now):
    return pacific_day(now)-timedelta(days=max(2, settings.analytics_lag_days))


# ----------------------------------------------------------------------------- seeds
def own_vocabulary(session, videos, tags_by_video):
    vocab = {}
    for v in videos:
        words = set(tokens(v.title))|{t for tag in tags_by_video.get(v.id, []) for t in tokens(tag)}
        terms = session.scalars(select(DiscoverySignal.detail).where(DiscoverySignal.video_id == v.id, DiscoverySignal.kind == "own_search_term"))
        for term in terms:
            words |= set(tokens(term))
        vocab[v.id] = words
    return vocab


def retire_generic_seeds(session, today):
    """Seeds from before MIN_SEED_TOKENS ("pop", "note"): keep the row for audit, never probe it again.

    A single generic word is not a search intent, and probing it costs 100 quota units that a specific
    query needs. Real own search terms are exempt – YouTube reported them, so they are demand.
    """
    own_terms = {normalize(t) for t in session.scalars(select(DiscoverySignal.detail).where(DiscoverySignal.kind == "own_search_term"))}
    retired = 0
    for q in session.scalars(select(DiscoveryQuery).where(DiscoveryQuery.source != "own_search_term")):
        if q.query in own_terms or len(q.query.split()) >= MIN_SEED_TOKENS or q.priority == RETIRED_PRIORITY:
            continue
        q.priority, q.next_probe_day = RETIRED_PRIORITY, today+timedelta(days=3650)
        retired += 1
    if retired:
        session.commit()
    return retired


def seed_queries(session, videos, tags_by_video, today):
    """Candidate probes: real own search terms first, then brand-free title/tag phrases. Deduplicated by normalised text."""
    candidates = {}
    def add(query, source, video_id, priority):
        key = normalize(query)
        if len(key) < 3:
            return
        # Real own search terms may be single words; generated title/tag words may not.
        if source != "own_search_term" and len(key.split()) < MIN_SEED_TOKENS:
            return
        current = candidates.get(key)
        if current is None or priority > current["priority"]:
            candidates[key] = {"query": key, "source": source, "seed_video_id": video_id, "priority": priority}
    for v in videos:
        for row in session.execute(select(DiscoverySignal.detail, DiscoverySignal.views).where(DiscoverySignal.video_id == v.id,
                DiscoverySignal.kind == "own_search_term").order_by(DiscoverySignal.views.desc()).limit(15)):
            add(row[0], "own_search_term", v.id, 1000+row[1])
        words = tokens(v.title)
        if words:
            add(" ".join(words[:4]), "title", v.id, 500)
            for a, b in zip(words, words[1:]):
                add(f"{a} {b}", "title_bigram", v.id, 300)
        for tag in tags_by_video.get(v.id, [])[:8]:
            add(tag, "tag", v.id, 200)
    retire_generic_seeds(session, today)
    existing = {row[0]: row[1] for row in session.execute(select(DiscoveryQuery.query, DiscoveryQuery.priority))}
    for c in candidates.values():
        priority = max(c["priority"], existing.get(c["query"], 0.0))
        statement = upsert(session, DiscoveryQuery).values(query=c["query"], source=c["source"], seed_video_id=c["seed_video_id"],
            priority=priority, created_at=utcnow(), probe_count=0, failures=0, results={})
        session.execute(statement.on_conflict_do_update(index_elements=["query"], set_={"priority": statement.excluded.priority}))
    session.commit()
    return len(candidates)


# ----------------------------------------------------------------------------- collection
def _item_values(item, today, via):
    snippet, stats = item.get("snippet", {}), item.get("statistics", {})
    duration = item.get("contentDetails", {}).get("duration")
    return dict(video_id=item["id"], channel_id=snippet.get("channelId"), title=snippet.get("title", "")[:512],
        channel_title=(snippet.get("channelTitle") or "")[:256],
        published_at=datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00")) if snippet.get("publishedAt") else None,
        duration_seconds=isodate.parse_duration(duration).total_seconds() if duration else None,
        views=int(stats["viewCount"]) if "viewCount" in stats else None, likes=int(stats["likeCount"]) if "likeCount" in stats else None,
        comments=int(stats["commentCount"]) if "commentCount" in stats else None, tags=(snippet.get("tags") or [])[:30], via=via,
        first_seen_day=today, last_seen_day=today, seen_count=1)


def _store_items(session, items, today, via_key, via_value):
    for item in items:
        existing = session.get(DiscoveryItem, item["id"])
        via = dict(existing.via) if existing else {}
        via.setdefault(via_key, [])
        if via_value not in via[via_key]:
            via[via_key] = (via[via_key]+[via_value])[-20:]
        values = _item_values(item, today, via)
        statement = upsert(session, DiscoveryItem).values(**values)
        session.execute(statement.on_conflict_do_update(index_elements=["video_id"], set_={
            "title": statement.excluded.title, "channel_id": statement.excluded.channel_id, "channel_title": statement.excluded.channel_title,
            "views": statement.excluded.views, "likes": statement.excluded.likes, "comments": statement.excluded.comments,
            "tags": statement.excluded.tags, "via": statement.excluded.via, "last_seen_day": statement.excluded.last_seen_day,
            "seen_count": DiscoveryItem.seen_count+1}))


def collect_signals(session, client, videos, now, budget, stats):
    end = analytics_end(now)
    start = end-timedelta(days=SIGNAL_WINDOW_DAYS-1)
    today = pacific_day(now)
    for v in videos:
        for source, kind in SOURCE_KINDS.items():
            if budget:
                budget.check()
            latest = session.scalar(select(func.max(DiscoverySignal.window_end)).where(DiscoverySignal.video_id == v.id, DiscoverySignal.kind == kind))
            if latest == end:
                continue  # Today's window already stored: idempotent.
            rows = client.traffic_detail(v.id, start, end, source)
            for row in rows:
                detail = str(row.get("insightTrafficSourceDetail", ""))[:512]
                if not detail:
                    continue
                statement = upsert(session, DiscoverySignal).values(video_id=v.id, kind=kind, detail=detail, window_start=start, window_end=end,
                    views=int(row.get("views", 0)), watch_minutes=float(row.get("estimatedMinutesWatched", 0) or 0), fetched_day=today)
                session.execute(statement.on_conflict_do_update(index_elements=["video_id", "kind", "detail", "window_end"],
                    set_={"views": statement.excluded.views, "watch_minutes": statement.excluded.watch_minutes}))
                stats["signals"] += 1
            session.commit()


def collect_embeds(session, client, videos, now, budget, stats):
    """Fremde Seiten, auf denen unser Video eingebettet lief – eigene Analytics, keine Data-API-Quota."""
    end = analytics_end(now)
    start = end-timedelta(days=SIGNAL_WINDOW_DAYS-1)
    today = pacific_day(now)
    if not hasattr(client, "embedded_locations"):
        return
    for video in videos:
        if budget:
            budget.check()
        latest = session.scalar(select(func.max(DiscoverySignal.window_end)).where(
            DiscoverySignal.video_id == video.id, DiscoverySignal.kind == "own_embed"))
        if latest == end:
            continue
        for row in client.embedded_locations(video.id, start, end):
            detail = str(row.get("insightPlaybackLocationDetail", ""))[:512]
            if not detail:
                continue
            statement = upsert(session, DiscoverySignal).values(
                video_id=video.id, kind="own_embed", detail=detail, window_start=start, window_end=end,
                views=int(row.get("views", 0)), watch_minutes=float(row.get("estimatedMinutesWatched", 0) or 0),
                fetched_day=today)
            session.execute(statement.on_conflict_do_update(
                index_elements=["video_id", "kind", "detail", "window_end"],
                set_={"views": statement.excluded.views, "watch_minutes": statement.excluded.watch_minutes}))
            stats["embeds"] = stats.get("embeds", 0)+1
        session.commit()


def pool_queries(session, videos=None, generic=None, limit=MAX_POOL_SEARCHES):
    """Suchauftraege aus belegten Audience-Intents – nicht aus einzelnen Tags oder Titelwoertern.

    Ein einzelner Tag ist kein Suchintent: „11am album teaser“ fand K-Pop und eine Kirchengemeinde,
    „good times“ eine Fernsehserie. Ein Intent ist ein belegter Themenkopf mit belegtem Kontext, und
    er bringt seine Belege mit (siehe app/audience.py).

    Verteilt wird je Video: der staerkste Intent sucht nach Playlists, der zweitstaerkste nach Kanaelen.
    Sonst verbraucht ein einzelnes Video beide Suchen und die anderen bekommen nie eine Chance.
    """
    from . import audience
    by_video, order = {}, []
    for intent in audience.intents(session, videos, generic):
        if intent["retired"]:
            continue
        if intent["video_id"] not in by_video:
            order.append(intent["video_id"])
        by_video.setdefault(intent["video_id"], []).append(intent)
    jobs = []
    for kind, index in (("playlist", 0), ("channel", 1)):
        for video_id in order:
            found = by_video[video_id]
            intent = found[index] if len(found) > index else found[0]
            # Eine Playlist soll zu Thema und Genre passen; ein Kanal soll das Publikum des Themas haben.
            # „trans mongolian“ findet „Mongolian Trains“, „trans mongolian ambient“ nicht mehr.
            head = intent["head"]
            query = " ".join(head) if kind == "channel" and len(head) >= 2 else intent["query"]
            jobs.append({"query": query, "source": intent["kind"], "intent": intent, "search": kind})
    seen, plans = set(), []
    for job in jobs:
        key = (job["search"], job["query"])
        if key in seen:
            continue
        seen.add(key)
        plans.append(job)
    return sorted(plans, key=lambda j: j["search"] != "playlist")[:limit]


def verify_pool_membership(session, client, quota, videos, stats, limit=40):
    """Fuer gefundene Playlists festhalten, ob unsere Musik schon darin liegt.

    Ohne diese Angabe darf keine Playlist angeschrieben werden: eine bestehende Platzierung ist eine
    gewachsene Beziehung, und im Zweifel wird nicht kontaktiert (siehe PRODUKTREGEL in app/acquisition.py).
    """
    if not hasattr(client, "playlist_contains"):
        return
    own_ids = [v.id for v in videos]
    checked, open_left = 0, 0
    # Die zuletzt gefundenen zuerst: sie sind die Kandidaten, die heute in der Queue landen koennten.
    # Vorher lief die Pruefung nach den Suchen und hinter allen Altfunden – dann war das Tagesbudget weg
    # und genau die neuen Playlists blieben ungeprueft.
    for pool in session.scalars(select(AudiencePool).where(AudiencePool.kind == "playlist")
                                .order_by(AudiencePool.last_seen_day.desc(), AudiencePool.id.desc())):
        details = pool.details or {}
        if "contains_own" in details:
            continue
        if checked >= limit or quota.remaining() < LIST_COST*(len(own_ids)+2):
            open_left += 1
            continue
        quota.spend(LIST_COST*len(own_ids))
        try:
            contains = bool(client.playlist_contains(pool.key, own_ids))
        except Exception:
            continue        # Unbekannt bleibt unbekannt – und unbekannt heisst: nicht ansprechen.
        pool.details = {**details, "contains_own": contains}
        checked += 1
        stats["membership_checked"] = stats.get("membership_checked", 0)+1
    stats["membership_open"] = open_left
    session.commit()
    log.info("discovery pools membership checked=%s open=%s units_left=%s", checked, open_left, quota.remaining())


def probe_audience_pools(session, client, quota, videos, now, budget, stats):
    """Fremde Playlists und Kanaele zu unserem Thema - Audiences, die uns noch nicht kennen.

    Eine kuratierte Playlist hat einen Betreiber, den man ansprechen kann, und eine nachpruefbare Groesse;
    ein Kanal hat Abonnenten. Beides sind Flaechen ausserhalb unserer bisherigen Reichweite. Die Suche
    kostet 100 Einheiten, deshalb hoechstens zwei je Lauf.

    Jeder Schritt wird protokolliert: welcher Query, wie viele Treffer, was gespeichert wurde und warum
    nicht. Ohne dieses Protokoll ist ein Nulllauf nicht von einem nicht stattgefundenen Lauf zu
    unterscheiden - genau das war vorher der Fall.
    """
    today = pacific_day(now)
    report = {"queries": [], "note": None, "stored": 0, "candidates": 0, "available_queries": []}
    stats["pool_probe"] = report
    if not hasattr(client, "search_playlists"):
        report["note"] = "Der YouTube-Client dieses Laufs kann keine Pool-Suche ausfuehren."
        return
    plans = pool_queries(session, videos)
    if not plans:
        report["note"] = ("Kein belastbarer Audience-Intent: aus Titel, Beschreibung, Tags, Themenkategorien, "
                          "realen Suchbegriffen und belegter Nachbarschaft ergibt sich derzeit kein Thema mit "
                          "Kontext, das nicht nur unsere eigene Verpackung beschreibt.")
        log.info("discovery pools skipped reason=%s", report["note"])
        return
    report["available_queries"] = [p["query"] for p in plans]
    report["intents"] = [{k: v for k, v in (p.get("intent") or {}).items() if k != "evidence"} for p in plans]
    own_channel = {v.channel_id for v in videos}

    def store(kind, key, title, url, **fields):
        # Geprueftes bleibt geprueft: ohne dieses Zusammenfuehren wuerde jeder neue Fund die
        # Mitgliedschaftspruefung und das letzte Verdikt ueberschreiben.
        previous = session.scalar(select(AudiencePool).where(AudiencePool.kind == kind, AudiencePool.key == key))
        keep = {k: v for k, v in ((previous.details or {}) if previous else {}).items()
                if k in ("contains_own", "verdict")}
        fields["details"] = {**keep, **(fields.get("details") or {})}
        statement = upsert(session, AudiencePool).values(kind=kind, key=key, title=(title or "")[:300],
                                                        url=url[:500], first_seen_day=today, last_seen_day=today,
                                                        **fields)
        session.execute(statement.on_conflict_do_update(index_elements=["kind", "key"], set_={
            "title": statement.excluded.title, "item_count": statement.excluded.item_count,
            "subscribers": statement.excluded.subscribers, "views": statement.excluded.views,
            "details": statement.excluded.details, "query": statement.excluded.query,
            "last_seen_day": statement.excluded.last_seen_day}))
        stats["pools"] = stats.get("pools", 0)+1
        report["stored"] += 1

    def intent_of(plan):
        intent = plan.get("intent") or {}
        return {k: intent.get(k) for k in ("key", "kind", "label", "head", "context", "attestations", "query",
                                           "video_id")}

    def entry_for(kind, plan):
        item = {"kind": kind, "query": plan["query"], "source": plan["source"], "results": 0, "own": 0,
                "stored": 0, "titles": [], "note": None, "intent": (plan.get("intent") or {}).get("label")}
        report["queries"].append(item)
        return item

    def search_playlists(plan, entry):
        if quota.remaining() < SEARCH_COST+LIST_COST*3:
            entry["note"] = f"Tagesbudget erschoepft: {quota.remaining()} Einheiten uebrig."
            return
        quota.spend(SEARCH_COST)
        found = client.search_playlists(plan["query"], max_results=MAX_POOL_ITEMS)
        entry["results"] = len(found)
        entry["titles"] = [str(p.get("title") or p.get("playlist_id")) for p in found][:MAX_POOL_ITEMS]
        entry["own"] = sum(1 for p in found if p.get("channel_id") in own_channel)
        ids = [p["playlist_id"] for p in found if p.get("channel_id") not in own_channel][:MAX_POOL_ITEMS]
        if not ids:
            entry["note"] = "Keine fremden Playlists in den Treffern."
            return
        quota.spend(LIST_COST)
        details = {p["id"]: p for p in client.playlists_by_id(ids)}
        # Die Groesse des Kurators gehoert zur Flaeche: eine Playlist ohne Publikum bringt keine Views.
        owners = {}
        owner_ids = [d["snippet"].get("channelId") for d in details.values() if d["snippet"].get("channelId")]
        if owner_ids and quota.remaining() > LIST_COST:
            quota.spend(LIST_COST)
            for item in client.channels_by_id(owner_ids):
                statistics = item.get("statistics", {})
                owners[item["id"]] = {
                    "subscribers": None if statistics.get("hiddenSubscriberCount") else
                                   (int(statistics["subscriberCount"]) if "subscriberCount" in statistics else None),
                    "views": int(statistics["viewCount"]) if "viewCount" in statistics else None}
        for candidate in found:
            meta = details.get(candidate.get("playlist_id"))
            if meta is None:
                continue
            counts = meta.get("contentDetails", {})
            members = []
            if quota.remaining() > LIST_COST:
                quota.spend(LIST_COST)
                members = [i["snippet"]["title"] for i in client.playlist_items(candidate["playlist_id"], 10)
                           if i.get("snippet")]
            owner = owners.get(meta["snippet"].get("channelId")) or {}
            store("playlist", candidate["playlist_id"], meta["snippet"]["title"],
                  f"https://www.youtube.com/playlist?list={candidate['playlist_id']}",
                  channel_id=meta["snippet"].get("channelId"), channel_title=meta["snippet"].get("channelTitle"),
                  item_count=counts.get("itemCount"), subscribers=owner.get("subscribers"), views=owner.get("views"),
                  description=(meta["snippet"].get("description") or "")[:1000],
                  published_at=None, query=plan["query"],
                  details={"items": members[:10], "privacy": (meta.get("status") or {}).get("privacyStatus"),
                           "query_source": plan["source"], "intent": intent_of(plan)})
            entry["stored"] += 1
        session.commit()

    def search_channels(plan, entry):
        if quota.remaining() < SEARCH_COST+LIST_COST:
            entry["note"] = f"Tagesbudget erschoepft: {quota.remaining()} Einheiten uebrig."
            return
        quota.spend(SEARCH_COST)
        found = client.search_channels(plan["query"], max_results=MAX_POOL_ITEMS)
        entry["results"] = len(found)
        entry["titles"] = [str(c.get("title") or c.get("channel_id")) for c in found][:MAX_POOL_ITEMS]
        entry["own"] = sum(1 for c in found if c.get("channel_id") in own_channel)
        ids = [c["channel_id"] for c in found if c["channel_id"] not in own_channel][:MAX_POOL_ITEMS]
        if not ids:
            entry["note"] = "Keine fremden Kanaele in den Treffern."
            return
        quota.spend(LIST_COST)
        for item in client.channels_by_id(ids):
            statistics = item.get("statistics", {})
            branding = (item.get("brandingSettings", {}) or {}).get("channel", {})
            hidden = statistics.get("hiddenSubscriberCount")
            store("channel", item["id"], item.get("snippet", {}).get("title", ""),
                  f"https://www.youtube.com/channel/{item['id']}",
                  channel_id=item["id"], channel_title=item.get("snippet", {}).get("title"),
                  item_count=int(statistics["videoCount"]) if "videoCount" in statistics else None,
                  subscribers=None if hidden else (int(statistics["subscriberCount"]) if "subscriberCount" in statistics else None),
                  views=int(statistics["viewCount"]) if "viewCount" in statistics else None,
                  description=(item.get("snippet", {}).get("description") or "")[:1000], published_at=None,
                  query=plan["query"],
                  details={"topics": (item.get("topicDetails", {}) or {}).get("topicCategories", []),
                           "keywords": (branding.get("keywords") or "")[:500],
                           "country": item.get("snippet", {}).get("country"),
                           "query_source": plan["source"], "intent": intent_of(plan)})
            entry["stored"] += 1
        session.commit()

    for plan in plans:
        if budget:
            budget.check()
        entry = entry_for(plan["search"], plan)
        (search_playlists if plan["search"] == "playlist" else search_channels)(plan, entry)

    report["candidates"] = sum(e["results"] for e in report["queries"])
    for item in report["queries"]:
        log.info("discovery pools kind=%s query=%r source=%s results=%s own=%s stored=%s note=%s titles=%r",
                 item["kind"], item["query"], item["source"], item["results"], item["own"], item["stored"],
                 item["note"], " | ".join(item["titles"][:6])[:400])


def collect_neighbors(session, client, quota, now, budget, stats):
    """Public metadata for videos that already recommend ours (real suggested neighbours)."""
    today = pacific_day(now)
    sources = set(session.scalars(select(DiscoverySignal.detail).where(DiscoverySignal.kind == "own_suggested_source")))
    known = {row[0]: row[1] for row in session.execute(select(DiscoveryItem.video_id, DiscoveryItem.last_seen_day))}
    due = [s for s in sources if re.fullmatch(r"[A-Za-z0-9_-]{11}", s) and (s not in known or known[s] <= today-timedelta(days=REPROBE_DAYS))]
    for offset in range(0, len(due), 50):
        if budget:
            budget.check()
        batch = due[offset:offset+50]
        quota.spend(LIST_COST)
        _store_items(session, client.videos_by_id(batch), today, "suggested_source", "own_traffic")
        stats["neighbors"] += len(batch)
        session.commit()


def probe_queries(session, client, quota, videos, now, budget, stats):
    today = pacific_day(now)
    own_ids = {v.id for v in videos}
    due = list(session.scalars(select(DiscoveryQuery).where((DiscoveryQuery.next_probe_day.is_(None)) | (DiscoveryQuery.next_probe_day <= today))
                               .order_by(DiscoveryQuery.priority.desc(), DiscoveryQuery.id).limit(MAX_SEARCHES_PER_RUN)))
    for q in due:
        if budget:
            budget.check()
        if quota.remaining() < SEARCH_COST+LIST_COST:
            raise QuotaExhausted()
        if quota.remaining() < SEARCH_COST+LIST_COST+min(POOL_RESERVE, int(quota.limit*POOL_RESERVE_SHARE)):
            # Die Suchproben duerfen die Pool-Suche nicht aushungern: sie laeuft als letzter Schritt und
            # verlor bisher genau die Einheiten, die hier vorher verbraucht wurden.
            stats["pool_reserve_kept"] = min(POOL_RESERVE, int(quota.limit*POOL_RESERVE_SHARE))
            break
        try:
            quota.spend(SEARCH_COST)
            results = client.search(q.query, max_results=25)
            ids = [r["video_id"] for r in results]
            details = []
            if ids:
                quota.spend(LIST_COST)
                details = client.videos_by_id(ids)
            _store_items(session, details, today, "queries", q.query)
            by_id = {d["id"]: d for d in details}
            views = [int(by_id[i]["statistics"].get("viewCount", 0)) for i in ids if i in by_id]
            published = [datetime.fromisoformat(by_id[i]["snippet"]["publishedAt"].replace("Z", "+00:00")) for i in ids if i in by_id]
            recent = sum(1 for p in published if (aware(now)-p).days <= 365)
            channels = {r["channel_id"] for r in results}
            our_rank = next((i+1 for i, vid in enumerate(ids) if vid in own_ids), None)
            q.results = {"n": len(ids), "median_views": float(median(views)) if views else None,
                         "recent_share": recent/len(published) if published else None,
                         "channel_diversity": len(channels)/len(ids) if ids else None,
                         "big_share": sum(v > 100000 for v in views)/len(views) if views else None,
                         "our_rank": our_rank, "top_ids": ids[:10], "probed_day": str(today)}
            q.last_probed_day, q.next_probe_day, q.probe_count = today, today+timedelta(days=REPROBE_DAYS), q.probe_count+1
            stats["searches"] += 1
            session.commit()
        except (SyncBudgetExceeded, QuotaExhausted):
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            if classify_error(exc) == "throttled":
                raise Throttled() from exc
            q = session.get(DiscoveryQuery, q.id)
            q.failures += 1
            q.next_probe_day = today+timedelta(days=FAIL_BACKOFF_DAYS*q.failures)
            session.commit()
            stats["failures"] += 1


def collect_playlists(session, client, quota, now, budget, stats):
    """Inventory of our own playlists (1 unit). Only what is listed here may be used as an existing resource."""
    if budget:
        budget.check()
    if not hasattr(client, "own_playlists"):
        return None
    today = pacific_day(now)
    quota.spend(LIST_COST)
    seen = []
    for item in client.own_playlists():
        snippet, details = item.get("snippet", {}), item.get("contentDetails", {})
        statement = upsert(session, ChannelPlaylist).values(id=item["id"], title=(snippet.get("title") or "")[:256],
            item_count=details.get("itemCount"), privacy=(item.get("status", {}) or {}).get("privacyStatus"),
            first_seen_day=today, last_seen_day=today, checked_at=utcnow())
        session.execute(statement.on_conflict_do_update(index_elements=["id"], set_={
            "title": statement.excluded.title, "item_count": statement.excluded.item_count,
            "privacy": statement.excluded.privacy, "last_seen_day": statement.excluded.last_seen_day,
            "checked_at": statement.excluded.checked_at}))
        seen.append(item["id"])
    stats["playlists"] = len(seen)
    stats["playlists_checked_day"] = str(today)
    session.commit()
    return seen


def collect_channels(session, client, quota, now, budget, stats):
    today = pacific_day(now)
    ids = set(x for x in session.scalars(select(DiscoveryItem.channel_id)) if x)
    known = {row[0]: row[1] for row in session.execute(select(DiscoveryChannel.channel_id, DiscoveryChannel.last_seen_day))}
    due = [c for c in ids if c not in known or known[c] <= today-timedelta(days=CHANNEL_REFRESH_DAYS)]
    for offset in range(0, len(due), 50):
        if budget:
            budget.check()
        quota.spend(LIST_COST)
        for item in client.channels_by_id(due[offset:offset+50]):
            stats_ = item.get("statistics", {})
            statement = upsert(session, DiscoveryChannel).values(channel_id=item["id"], title=item.get("snippet", {}).get("title", "")[:256],
                subscribers=int(stats_["subscriberCount"]) if "subscriberCount" in stats_ and not stats_.get("hiddenSubscriberCount") else None,
                video_count=int(stats_["videoCount"]) if "videoCount" in stats_ else None, views=int(stats_["viewCount"]) if "viewCount" in stats_ else None,
                first_seen_day=today, last_seen_day=today)
            session.execute(statement.on_conflict_do_update(index_elements=["channel_id"], set_={"title": statement.excluded.title,
                "subscribers": statement.excluded.subscribers, "video_count": statement.excluded.video_count, "views": statement.excluded.views,
                "last_seen_day": statement.excluded.last_seen_day}))
            stats["channels"] += 1
        session.commit()


def own_tags(session, client, quota, videos, budget, now=None):
    """Eigene oeffentliche Metadaten: Tags, Beschreibung, YouTube-Themenkategorien – und der eigene Kanal.

    Das kostet zwei Einheiten fuer alle Videos zusammen. Bisher wurden die Tags nur fluechtig im Lauf
    benutzt und danach weggeworfen; damit bestand unser Thema fuer jede spaetere Schicht aus einem
    einzigen Titelwort. Jetzt liegen die Angaben in der Datenbank und tragen die Audience-Intents.
    """
    if budget:
        budget.check()
    today = pacific_day(now or utcnow())
    quota.spend(LIST_COST)
    result = {}
    items = client.videos_by_id([v.id for v in videos], part="snippet,topicDetails")
    channel_ids = {v.channel_id for v in videos if v.channel_id}
    channels = {}
    if channel_ids and hasattr(client, "channels_by_id") and quota.remaining() > LIST_COST:
        quota.spend(LIST_COST)
        for item in client.channels_by_id(sorted(channel_ids)):
            branding = (item.get("brandingSettings", {}) or {}).get("channel", {})
            channels[item["id"]] = {
                "title": item.get("snippet", {}).get("title"),
                "description": (item.get("snippet", {}).get("description") or "")[:2000],
                "keywords": (branding.get("keywords") or "")[:1000],
                "topics": (item.get("topicDetails", {}) or {}).get("topicCategories", [])}
    by_video = {v.id: v for v in videos}
    for item in items:
        snippet = item.get("snippet", {})
        result[item["id"]] = (snippet.get("tags") or [])[:30]
        video = by_video.get(item["id"])
        channel = channels.get(video.channel_id if video else None) or {}
        statement = upsert(session, VideoProfile).values(
            video_id=item["id"], description=(snippet.get("description") or "")[:5000],
            tags=result[item["id"]], topics=(item.get("topicDetails", {}) or {}).get("topicCategories", []),
            category_id=snippet.get("categoryId"), channel_title=channel.get("title") or snippet.get("channelTitle"),
            channel_description=channel.get("description"), channel_keywords=channel.get("keywords"),
            channel_topics=channel.get("topics", []), fetched_day=today)
        session.execute(statement.on_conflict_do_update(index_elements=["video_id"], set_={
            "description": statement.excluded.description, "tags": statement.excluded.tags,
            "topics": statement.excluded.topics, "category_id": statement.excluded.category_id,
            "channel_title": statement.excluded.channel_title,
            "channel_description": statement.excluded.channel_description,
            "channel_keywords": statement.excluded.channel_keywords,
            "channel_topics": statement.excluded.channel_topics, "fetched_day": statement.excluded.fetched_day}))
    session.commit()
    return result


def run(client, now=None, budget=None, force=False):
    """Daily discovery pass; own lease, quota-accounted, resumable. Never touches core sync tables."""
    now = now or utcnow()
    owner, skipped = acquire(Session, None, LEASE)
    if skipped:
        return {"status": skipped}
    try:
        return _run(client, now, budget or Budget(settings.sync_budget_seconds), force)
    finally:
        release(Session, owner, None, LEASE)


def _run(client, now, budget, force):
    today = pacific_day(now)
    issues, stats = [], {"signals": 0, "neighbors": 0, "searches": 0, "channels": 0, "failures": 0, "opportunities": 0,
                         "evaluated": 0, "playlists": None, "pools": 0, "embeds": 0}
    with Session() as s:
        done_today = s.scalar(select(DiscoveryRun).where(DiscoveryRun.day == today, DiscoveryRun.status.in_(["ok", "throttled", "quota_exhausted"])))
        if done_today and not force:
            return {"status": "already_completed", "day": str(today)}
        run_row = DiscoveryRun(day=today)
        s.add(run_row)
        s.commit()
        run_id = run_row.id
    status = "ok"
    try:
        with Session() as s:
            quota = Quota(s, today)
            videos = list(s.scalars(select(Video).where(Video.active.is_(True))))
            collect_signals(s, client, videos, now, budget, stats)
            try:
                collect_embeds(s, client, videos, now, budget, stats)
            except (SyncBudgetExceeded, QuotaExhausted, Throttled):
                raise
            except Exception as exc:
                s.rollback()
                issues.append(f"embeds: {type(exc).__name__}")
            tags = own_tags(s, client, quota, videos, budget, now)
            try:
                collect_playlists(s, client, quota, now, budget, stats)
            except (SyncBudgetExceeded, QuotaExhausted, Throttled):
                raise
            except Exception as exc:
                # Ohne Inventar bleibt die Playlist-Existenz unbekannt; der Rest des Laufs ist davon unberührt.
                s.rollback()
                issues.append(f"playlists: {type(exc).__name__}")
            collect_neighbors(s, client, quota, now, budget, stats)
            seed_queries(s, videos, tags, today)
            probe_queries(s, client, quota, videos, now, budget, stats)
            collect_channels(s, client, quota, now, budget, stats)
            try:
                verify_pool_membership(s, client, quota, videos, stats)
                probe_audience_pools(s, client, quota, videos, now, budget, stats)
                verify_pool_membership(s, client, quota, videos, stats)
            except (SyncBudgetExceeded, QuotaExhausted, Throttled):
                raise
            except Exception as exc:
                s.rollback()
                issues.append(f"pools: {type(exc).__name__}")
                log.info("discovery pools failed (%s)", type(exc).__name__)
            s.commit()
            stats["units_used"] = quota.spent_now
            stats["units_today"] = quota.used
    except SyncBudgetExceeded:
        status = "deferred"
        issues.append("Zeitbudget erreicht; Discovery wird beim nächsten Lauf fortgesetzt.")
    except QuotaExhausted:
        status = "quota_exhausted"
        issues.append(f"Tagesbudget von {DAILY_UNITS} Data-API-Einheiten erreicht; Rest folgt morgen.")
    except Throttled:
        status = "throttled"
        issues.append("Google-Quota oder Störung; Fortschritt gespeichert, später erneut.")
    except Exception as exc:
        status = "failed"
        issues.append(f"discovery: {type(exc).__name__}")
        log.error("Discovery failed (%s); raw data omitted", type(exc).__name__)
        log.debug("discovery traceback", exc_info=True)
    try:
        with Session() as s:
            stats["opportunities"] = analyze(s, now)
            stats["evaluated"] = evaluate_memory(s, now)
            s.commit()
    except Exception as exc:
        issues.append(f"discovery/analyze: {type(exc).__name__}")
        if status == "ok":
            status = "partial"
    with Session() as s:
        row = s.get(DiscoveryRun, run_id)
        row.finished_at, row.status, row.issues, row.stats, row.units_used = utcnow(), status, issues, stats, stats.get("units_used", 0)
        s.commit()
    return {"status": status, "issues": issues, "stats": stats, "day": str(today)}


# ----------------------------------------------------------------------------- analysis
def _component(name, signal, weight, value=None, note=None, proxy=False, scoring=True):
    return {"name": name, "signal": None if signal is None else round(float(signal), 4), "weight": weight, "value": value,
            "available": signal is not None, "note": note, "evidence": "proxy" if proxy else "own_analytics",
            "scoring": scoring}


def _score(components):
    usable = [c for c in components if c["available"] and c.get("scoring", True)]
    if not usable:
        return None
    return round(50+50*sum(c["weight"]*c["signal"] for c in usable)/sum(c["weight"] for c in usable), 1)


def classify_evidence(families, circular=False, generic=False):
    """Evidence level from independent families. One proxy alone never justifies an experiment.

    generic: a single-word seed without own search-term demand is not a search intent, whatever else exists.
    circular: the seed comes from our own title/tags; proxies alone then stay weak, because relevance to
    ourselves is not demand. Own analytics or corroboration by several independent channels lifts it.
    """
    families = set(families)
    if "own_term_demand" in families:
        return "own_analytics"     # Real own demand for exactly this term; specificity is then proven, not assumed.
    if generic:
        return "none"
    independent = {f for f in families if f != "own_term_demand"}
    if len(independent) >= MIN_PROXY_FAMILIES and (not circular or independent & {"own_traffic_mix", "cluster_corroboration"}):
        return "multi_signal_proxy"
    return "weak_proxy" if independent else "none"


def item_routes(item, has_metadata):
    """On which independent routes did this neighbour video become visible?

    Discovered through our own recommending-source traffic and additionally described by public
    metadata is one route; appearing in a public search probe is another. The same probe never
    counts twice, no matter how many of its numbers are used.
    """
    via = item.via or {}
    routes = set()
    if has_metadata and via.get("suggested_source"):
        routes.add("neighbour_metadata")
    if via.get("queries"):
        routes.add("search_probe")
    return routes


def topical_context(shared_tokens, level, relevance=0.0, members=1, channels=1, own_data=False):
    """May this chance dictate the wording for description or playlist – or is it only a hypothesis?

    Token overlap is not topical identity. Either our own analytics prove that viewers actually reach
    us through this term, or several independent neighbour videos from different channels corroborate
    the same theme with more than one shared word. Everything else stays a hypothesis to be probed.
    """
    shared = [t for t in (shared_tokens or []) if t not in BRAND]
    if level == "own_analytics":
        return True, "Eigene Analytics belegen, dass Zuschauer über dieses Thema tatsächlich hier ankommen."
    if len(shared) < MIN_CONTEXT_TOKENS:
        return False, (f"Nur {len(shared)} gemeinsames Stichwort ({', '.join(shared) or 'keines'}): Wortgleichheit ist keine "
                       "Themengleichheit – als Hypothese behandeln, nicht als Textvorlage.")
    if relevance < MIN_CONTEXT_RELEVANCE:
        return False, f"Themenüberdeckung {relevance:.2f} unter {MIN_CONTEXT_RELEVANCE}: als Hypothese behandeln."
    if own_data:
        return True, (f"Eigene Views aus dieser Quelle und {len(shared)} gemeinsame Begriffe "
                      f"({', '.join(shared[:4])}): thematisch plausibel.")
    if members < MIN_CONTEXT_MEMBERS or channels < MIN_CONTEXT_CHANNELS:
        return False, (f"Nur {members} Nachbarvideo(s) aus {channels} Kanal/Kanälen: zu wenig unabhängige Bestätigung "
                       f"(nötig {MIN_CONTEXT_MEMBERS} aus {MIN_CONTEXT_CHANNELS}) – als Hypothese behandeln.")
    return True, (f"{members} Nachbarvideos aus {channels} Kanälen teilen {len(shared)} Begriffe "
                  f"({', '.join(shared[:4])}): thematisch plausibel.")


def demand_source(families):
    """Provenance in one word, so no proxy is ever read as measured demand."""
    if "own_term_demand" in families:
        return "own_analytics"
    if "own_traffic_mix" in families:
        return "own_traffic_plus_public_proxy"
    return "public_proxy"


def uncertainty(level, families):
    return {"own_analytics": "moderat", "multi_signal_proxy": "mittel"}.get(level, "hoch") if families else "hoch"


def grade(score, level, level_weights=None):
    """Cap a chance by its evidence level and by what this level has actually delivered so far."""
    if level == "none" or score is None:
        return None
    weight = (level_weights or {}).get(level, 1.0)
    return round(min(LEVEL_CAPS.get(level, PROXY_SCORE_CAP), score*weight), 1)


def seed_quality(key, source, own_terms):
    """Seed provenance and specificity: generic one-worders and self-derived terms are marked, not scored."""
    parts = tokens(key, keep_brand=True)
    is_own_term = key in own_terms or source == "own_search_term"
    return {"tokens": len(parts), "source": source, "is_own_search_term": is_own_term,
            "generic": len(parts) < MIN_SEED_TOKENS and not is_own_term,
            "circular": source in ("title", "title_bigram", "tag"),
            "note": ("Ein-Wort-Seed ohne eigene Suchnachfrage: keine Suchintention." if len(parts) < MIN_SEED_TOKENS and not is_own_term
                     else "Aus eigenem Titel/Tag abgeleitet: Relevanz zu uns ist keine Nachfrage." if source in ("title", "title_bigram", "tag")
                     else "Realer eigener Suchbegriff aus Analytics." if is_own_term else None)}


def missing_evidence(families, generic=False):
    """Which independent families are absent – the input for a low-risk evidence-gathering experiment."""
    return [{"family": name, "what": label} for name, label in FAMILIES.items() if name not in families] if not generic else \
           [{"family": "specific_seed", "what": "Ein spezifischerer Suchbegriff als ein einzelnes Wort"}]


def own_source_views(session, end):
    """Real own views per traffic source over the signal window: own analytics without term detail."""
    start = end-timedelta(days=SIGNAL_WINDOW_DAYS-1)
    out = defaultdict(dict)
    for video_id, source, views in session.execute(
            select(TrafficDaily.video_id, TrafficDaily.source, func.coalesce(func.sum(TrafficDaily.views), 0))
            .where(TrafficDaily.day >= start, TrafficDaily.day <= end, TrafficDaily.paid.is_(False))
            .group_by(TrafficDaily.video_id, TrafficDaily.source)):
        if views:
            out[video_id][source] = int(views)
    return out


def _latest_signals(session, kind):
    """Latest window per video: {video_id: {detail: views}} and the window end."""
    out, ends = defaultdict(dict), {}
    for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.kind == kind)):
        if row.video_id not in ends or row.window_end > ends[row.video_id]:
            ends[row.video_id] = row.window_end
            out[row.video_id] = {}
        if row.window_end == ends[row.video_id]:
            out[row.video_id][row.detail] = row.views
    return out, ends


def own_context(session, now):
    histories = load_histories(session)
    today = pacific_day(now)
    dataset = session.scalar(select(LearningDataset).order_by(LearningDataset.built_at.desc()))
    base = dataset.baselines if dataset else {"status": "insufficient_data", "medians": {}}
    ctx = {}
    for h in histories:
        f = features_at(h, today)
        paid = paid_profile(h, today)
        ctx[h.video.id] = {"video": h.video, "features": f, "paid": paid["status"], "paid_profile": paid,
                           "tokens": set(tokens(h.video.title)), "title_tokens": set(tokens(h.video.title))}
    tags = {}
    for row in session.scalars(select(DiscoveryItem).where(DiscoveryItem.video_id.in_(list(ctx)))):
        tags[row.video_id] = row.tags
    vocab = own_vocabulary(session, [c["video"] for c in ctx.values()], tags)
    for vid, c in ctx.items():
        c["vocab"] = vocab.get(vid, c["tokens"])|c["tokens"]
    return ctx, base


MIN_DECIDED_FOR_WEIGHT = 5   # Below this, an observed rate is noise: the weight stays neutral and says so.


def _weight_from(record):
    """Bounded prior 0.85–1.15 per bucket; neutral until enough decided outcomes exist."""
    weights = {}
    for key, e in record.items():
        decided = e["positive"]+e["negative"]+e["neutral"]
        e["decided"] = decided
        if decided >= MIN_DECIDED_FOR_WEIGHT:
            weights[key] = round(0.85+0.3*e["positive"]/decided, 3)
            e["basis"] = f"{e['positive']} von {decided} entschiedenen Fällen positiv (beobachtet, nicht kausal)."
        else:
            e["basis"] = f"Nur {decided} entschiedene Fälle (< {MIN_DECIDED_FOR_WEIGHT}): Gewicht bleibt neutral, nichts bewiesen."
        e["weight"] = weights.get(key, 1.0)
    return weights


def memory_weights(session):
    """Observed track record per opportunity kind and per evidence level → bounded priors; descriptive, not causal."""
    rows = list(session.scalars(select(DiscoveryOpportunity).where(DiscoveryOpportunity.status == "evaluated")))
    record, by_level = {}, {}
    for r in rows:
        outcome = r.outcome or "inconclusive"
        for bucket, key in ((record, r.kind), (by_level, (r.evidence or {}).get("evidence_level") or "unknown")):
            e = bucket.setdefault(key, {"positive": 0, "negative": 0, "neutral": 0, "inconclusive": 0, "n": 0})
            e[outcome] = e.get(outcome, 0)+1
            e["n"] += 1
    weights, level_weights = _weight_from(record), _weight_from(by_level)
    return weights, level_weights, {"by_kind": record, "by_evidence_level": by_level}


def match_video(query_tokens, ctx):
    best, best_rel = None, 0.0
    for vid, c in ctx.items():
        rel = overlap(query_tokens, c["vocab"])
        if rel > best_rel:
            best, best_rel = vid, rel
    return best, best_rel


def analyze(session, now):
    """Turn signals, probes and public metadata into scored, classified opportunities for today."""
    today = pacific_day(now)
    ctx, base = own_context(session, now)
    if not ctx:
        return 0
    search_signals, _ = _latest_signals(session, "own_search_term")
    suggested_signals, _ = _latest_signals(session, "own_suggested_source")
    max_search_views = max([v for per in search_signals.values() for v in per.values()] or [1])
    max_suggested_views = max([v for per in suggested_signals.values() for v in per.values()] or [1])
    weights, level_weights, _ = memory_weights(session)
    # Reale eigene Views je Traffic-Quelle: vorhanden auch dort, wo YouTube keine Begriffsdetails liefert.
    source_views = own_source_views(session, analytics_end(now))
    own_terms_normalised = {normalize(t) for per in search_signals.values() for t in per}
    medians = base.get("medians", {})
    conv_ref = medians.get("subscriber_conversion_7d", {})
    items = {row.video_id: row for row in session.scalars(select(DiscoveryItem)) if row.video_id not in ctx}
    channels = {row.channel_id: row for row in session.scalars(select(DiscoveryChannel))}
    written = 0

    def paid_note(vid):
        return ctx[vid]["paid"] not in ("organic", "organic_with_paid_history")

    def subscriber_fit(vid, relevance):
        f = ctx[vid]["features"] if vid else None
        conv = f.get("subscriber_conversion_7d") if f else None
        if conv is None or conv_ref.get("median") is None or conv_ref.get("n", 0) < 30 or paid_note(vid):
            return None
        return round(50+50*(0.5*tanh((conv-conv_ref["median"])/conv_ref["median"]) if conv_ref["median"] else 0)+25*(2*relevance-1), 1)

    def write(kind, key, vid, gap, scores, components, evidence):
        nonlocal written
        statement = upsert(session, DiscoveryOpportunity).values(day=today, kind=kind, key=key[:200], video_id=vid, gap=gap, scores=scores,
            components=components, evidence=evidence, status="open")
        session.execute(statement.on_conflict_do_update(index_elements=["day", "kind", "key"], set_={"video_id": statement.excluded.video_id,
            "gap": statement.excluded.gap, "scores": statement.excluded.scores, "components": statement.excluded.components, "evidence": statement.excluded.evidence}))
        written += 1

    # ---- search opportunities: probed queries plus own search terms without a probe yet
    seen = set()
    queries = list(session.scalars(select(DiscoveryQuery)))
    own_terms = {term for per in search_signals.values() for term in per}
    candidates = [(q.query, q.results or {}, q.source) for q in queries]
    for term in own_terms:
        key = normalize(term)
        if key and key not in {c[0] for c in candidates}:
            candidates.append((key, {}, "own_search_term"))
    for key, results, source in candidates:
        if key in seen:
            continue
        seen.add(key)
        qt = tokens(key, keep_brand=True)
        vid, relevance = match_video(qt, ctx)
        real_views = sum(views for per in search_signals.values() for term, views in per.items() if normalize(term) == key)
        real_ok = real_views > 0 and vid is not None and not paid_note(vid)
        comps = [
            _component("Relevanz zum Sealand-Video (Token-Abdeckung)", 2*relevance-1, 3, relevance,
                       "Zirkulär: Seeds stammen aus dem eigenen Titel/Tags – nur Filter, wird nicht gewertet", scoring=False),
            _component("Eigene Views aus diesem Suchbegriff (90 Tage, real)", (2*real_views/max_search_views-1) if real_ok else None, 3, real_views if real_views else None,
                       "Werbephase: nicht als organische Nachfrage gewertet" if (real_views and vid and paid_note(vid)) else None),
            _component("Nachfrage-Proxy: Median-Views der Top-Ergebnisse", tanh((log10(results["median_views"]+1)-4)/1.5) if results.get("median_views") is not None else None, 1.5,
                       results.get("median_views"), "kein Suchvolumen, nur Ergebnis-Reichweite", proxy=True),
            _component("Aktualität der Ergebnisse (Anteil < 1 Jahr)", 2*results["recent_share"]-1 if results.get("recent_share") is not None else None, 1, results.get("recent_share"), proxy=True),
            _component("Wettbewerb: Anteil Ergebnisse > 100k Views (niedriger = offener)", 1-2*results["big_share"] if results.get("big_share") is not None else None, 1.5, results.get("big_share"), proxy=True),
            _component("Kanalvielfalt der Ergebnisse", 2*results["channel_diversity"]-1 if results.get("channel_diversity") is not None else None, 1, results.get("channel_diversity"), proxy=True),
            _component("Eigener Rang in den Top-Ergebnissen", (1-results["our_rank"]/25) if results.get("our_rank") else (-0.5 if results.get("n") else None), 1.5, results.get("our_rank"),
                       "nicht in den Top-Ergebnissen" if results.get("n") and not results.get("our_rank") else None, proxy=True),
        ]
        score = _score(comps)
        if score is not None:
            score = round(min(100, score*weights.get("search", 1.0)), 1)
        quality = seed_quality(key, source, own_terms_normalised)
        families = set()
        if real_ok:
            families.add("own_term_demand")
        if vid and not paid_note(vid) and (source_views.get(vid, {}).get("YT_SEARCH") or 0) > 0:
            families.add("own_traffic_mix")
        if results.get("n"):
            # Ergebnismenge, Median-Views, Wettbewerb und eigener Rang stammen aus derselben Probe: eine Familie.
            families.add("search_probe")
        level = classify_evidence(families, circular=quality["circular"], generic=quality["generic"])
        score = grade(score, level, level_weights)
        missing_tokens = [t for t in qt if vid and t not in ctx[vid]["title_tokens"] and t not in BRAND]
        shared_tokens = [t for t in qt if vid and t in ctx[vid]["vocab"] and t not in BRAND]
        context_usable, context_reason = topical_context(shared_tokens, level, relevance,
                                                        own_data="own_traffic_mix" in families)
        if score is None or (relevance < MIN_RELEVANCE and not real_ok):
            gap = "insufficient_evidence"
        elif relevance >= .5 and results.get("n") and not results.get("our_rank"):
            gap = "existing_video_opportunity"
        elif relevance >= .5 and results.get("our_rank") and missing_tokens:
            gap = "packaging_opportunity"
        elif relevance >= .5:
            gap = "search_opportunity"
        elif relevance >= MIN_RELEVANCE and (real_ok or (results.get("median_views") or 0) > 10000):
            gap = "followup_content_opportunity"
        else:
            gap = "insufficient_evidence"
        evidence = {"demand_source": demand_source(families), "evidence_level": level,
                    "actionable": level in ACTIONABLE_LEVELS, "score_capped": level != "own_analytics",
                    "families": sorted(families), "family_labels": [FAMILIES[f] for f in sorted(families)],
                    "seed_quality": quality, "missing_evidence": missing_evidence(families, quality["generic"]),
                    "shared_tokens": shared_tokens, "context_usable": context_usable, "context_reason": context_reason,
                    "own_search_views_90d": real_views, "own_source_views_90d": source_views.get(vid, {}) if vid else {},
                    "probe": results,
                    "query_source": source, "missing_title_tokens": missing_tokens, "matched_video_id": vid, "relevance": relevance,
                    "missing": [c["name"] for c in comps if not c["available"]], "paid_status": ctx[vid]["paid"] if vid else None,
                    "uncertainty": uncertainty(level, families),
                    "baseline_views_for_memory": real_views, "note": SCORE_NOTE}
        write("search", key, vid, gap, {"search_opportunity_score": score, "subscriber_fit_score": subscriber_fit(vid, relevance) if vid else None,
                                        "external_audience_score": score}, {"components": comps}, evidence)

    # ---- suggested opportunities: recommending neighbours and search-result neighbours
    cluster_members = defaultdict(list)
    for ext_id, item in items.items():
        it = set(tokens(item.title))|{t for tag in item.tags for t in tokens(tag)}
        vid, relevance = match_video(list(it) or [""], ctx)
        real_views = sum(per.get(ext_id, 0) for per in suggested_signals.values())
        receiving = [v for v, per in suggested_signals.items() if per.get(ext_id)]
        if receiving:
            vid = max(receiving, key=lambda v: suggested_signals[v][ext_id])
        real_ok = real_views > 0 and vid is not None and not paid_note(vid)
        ch = channels.get(item.channel_id)
        subs = ch.subscribers if ch else None
        age_days = (aware(now)-aware(item.published_at)).days if item.published_at else None
        comps = [
            _component("Thematische Nähe (Titel/Tags vs eigenes Vokabular)", 2*relevance-1, 3, relevance),
            _component("Eigene Views über dieses Video als Empfehlung (90 Tage, real)", (2*real_views/max_suggested_views-1) if real_ok else None, 3, real_views if real_views else None,
                       "Werbephase: nicht als organische Nachfrage gewertet" if (real_views and vid and paid_note(vid)) else None),
            _component("Reichweite des Nachbarvideos (log Views)", tanh((log10((item.views or 0)+1)-4)/1.5) if item.views is not None else None, 1.5, item.views, proxy=True),
            _component("Kanalgröße erreichbar (1k–500k Abonnenten ideal)", (1-abs(log10(subs+1)-4.5)/3) if subs is not None else None, 1, subs, proxy=True),
            _component("Aktualität des Nachbarvideos", (0.5 if age_days <= 365 else -0.2 if age_days > 1500 else 0.1) if age_days is not None else None, 0.5, age_days, proxy=True),
        ]
        score = _score(comps)
        if score is not None:
            score = round(min(100, score*weights.get("suggested", 1.0)), 1)
        families = set()
        if real_ok:
            families.add("own_term_demand")
        if vid and not paid_note(vid) and (source_views.get(vid, {}).get("RELATED_VIDEO") or 0) > 0:
            families.add("own_traffic_mix")
        families |= item_routes(item, has_metadata=(item.views is not None or subs is not None))
        level = classify_evidence(families, circular=False)
        score = grade(score, level, level_weights)
        gap = "suggested_opportunity" if (relevance >= .3 or real_ok) and score is not None else "insufficient_evidence"
        shared = sorted(t for t in (it & (ctx[vid]["vocab"] if vid else set())) if t not in BRAND)
        context_usable, context_reason = topical_context(shared, level, relevance, members=1, channels=1,
                                                        own_data="own_traffic_mix" in families)
        label = " ".join(shared[:2]) if shared else (sorted(it)[:1] or ["unbekannt"])[0]
        cluster_members[label].append((ext_id, item, relevance, real_views, vid, shared))
        evidence = {"demand_source": demand_source(families), "evidence_level": level,
                    "actionable": level in ACTIONABLE_LEVELS, "score_capped": level != "own_analytics",
                    "families": sorted(families), "family_labels": [FAMILIES[f] for f in sorted(families)],
                    "missing_evidence": missing_evidence(families),
                    "own_source_views_90d": source_views.get(vid, {}) if vid else {},
                    "own_suggested_views_90d": real_views, "title": item.title,
                    "context_usable": context_usable, "context_reason": context_reason,
                    "channel": item.channel_title, "channel_subscribers": subs, "views": item.views, "age_days": age_days, "shared_tokens": shared[:6],
                    "matched_video_id": vid, "relevance": relevance, "missing": [c["name"] for c in comps if not c["available"]],
                    "paid_status": ctx[vid]["paid"] if vid else None, "via": item.via, "baseline_views_for_memory": real_views,
                    "uncertainty": uncertainty(level, families), "note": SCORE_NOTE+" Keine Aussage über Ursachen des fremden Erfolgs; nichts kopieren."}
        write("suggested", ext_id, vid, gap, {"suggested_opportunity_score": score, "subscriber_fit_score": subscriber_fit(vid, relevance) if vid else None,
                                              "external_audience_score": score}, {"components": comps}, evidence)

    # ---- audience clusters (neighbourhoods)
    for label, members in cluster_members.items():
        if len(members) < 2 or label == "unbekannt":
            continue
        rel = sum(m[2] for m in members)/len(members)
        reach = sum(log10((m[1].views or 0)+1) for m in members)/len(members)
        real = sum(m[3] for m in members)
        vids = [m[4] for m in members if m[4]]
        vid = max(set(vids), key=vids.count) if vids else None
        chans = {m[1].channel_id for m in members if m[1].channel_id}
        comps = [_component("Mittlere thematische Nähe", 2*rel-1, 3, rel), _component("Mittlere Reichweite (log Views)", tanh((reach-4)/1.5), 1.5, reach, proxy=True),
                 _component("Kanalvielfalt im Cluster", tanh(len(chans)/3-1), 1, len(chans), proxy=True),
                 _component("Eigene Views aus dem Cluster (real)", tanh(real/50) if real and vid and not paid_note(vid) else None, 2, real)]
        families = set()
        if real and vid and not paid_note(vid):
            families.add("own_term_demand")
        if vid and not paid_note(vid) and (source_views.get(vid, {}).get("RELATED_VIDEO") or 0) > 0:
            families.add("own_traffic_mix")
        routes = set()
        for m in members:
            routes |= item_routes(m[1], has_metadata=m[1].views is not None)
        families |= routes
        if len(chans) >= 2 and len(members) >= 3 and len(routes) >= 2:
            # Mehrere Kanäle, über mehrere Routen entdeckt: Korroboration statt eines einzelnen Proxys.
            families.add("cluster_corroboration")
        level = classify_evidence(families, circular=False)
        score = grade(_score(comps), level, level_weights)
        # Gemeinsam ist nur, was ALLE Clustermitglieder mit uns teilen: sonst traegt ein Zufallstreffer das Thema.
        common = sorted(set.intersection(*[set(m[5]) for m in members]) if members else set())
        context_usable, context_reason = topical_context(common, level, rel, members=len(members), channels=len(chans),
                                                        own_data="own_traffic_mix" in families)
        write("cluster", label, vid, "suggested_opportunity" if score and rel >= .3 else "insufficient_evidence",
              {"external_audience_score": score, "subscriber_fit_score": subscriber_fit(vid, rel) if vid else None},
              {"components": comps}, {"members": [{"video_id": m[0], "title": m[1].title, "views": m[1].views} for m in members[:8]], "n_members": len(members),
                                     "channels": len(chans), "matched_video_id": vid, "relevance": rel, "evidence_level": level,
                                     "actionable": level in ACTIONABLE_LEVELS, "score_capped": level != "own_analytics",
                                     "families": sorted(families), "family_labels": [FAMILIES[f] for f in sorted(families)],
                                     "missing_evidence": missing_evidence(families), "uncertainty": uncertainty(level, families),
                                     "demand_source": demand_source(families), "shared_tokens": common,
                                     "context_usable": context_usable, "context_reason": context_reason,
                                     "baseline_views_for_memory": real, "note": SCORE_NOTE})
    session.commit()
    return written


# ----------------------------------------------------------------------------- discovery memory
def evaluate_memory(session, now):
    """After MEMORY_DAYS: did own traffic from this search term / recommending video grow? Paid windows stay inconclusive."""
    today = pacific_day(now)
    search_signals, _ = _latest_signals(session, "own_search_term")
    suggested_signals, _ = _latest_signals(session, "own_suggested_source")
    ctx = {v.id: paid_profile(h, today)["status"] for h in load_histories(session) for v in [h.video]}
    evaluated = 0
    for row in session.scalars(select(DiscoveryOpportunity).where(DiscoveryOpportunity.status == "open", DiscoveryOpportunity.day <= today-timedelta(days=MEMORY_DAYS))):
        baseline = (row.evidence or {}).get("baseline_views_for_memory") or 0
        if row.kind == "search":
            current = sum(views for per in search_signals.values() for term, views in per.items() if normalize(term) == row.key)
        elif row.kind == "suggested":
            current = sum(per.get(row.key, 0) for per in suggested_signals.values())
        else:
            current = None
        paid = row.video_id and ctx.get(row.video_id) not in ("organic", "organic_with_paid_history", None)
        if current is None:
            outcome, detail = "inconclusive", "Cluster ohne direkte Traffic-Zuordnung."
        elif paid:
            outcome, detail = "inconclusive", "Werbetraffic im Auswertungsfenster."
        elif current >= max(5, baseline*1.25):
            outcome, detail = "positive", f"Eigene Views aus dieser Quelle {baseline} → {current} (beobachtet, nicht kausal)."
        elif baseline and current <= baseline*0.75:
            outcome, detail = "negative", f"Eigene Views aus dieser Quelle {baseline} → {current}; Chance verschwindet."
        elif baseline == 0 and current == 0:
            outcome, detail = "inconclusive", "Weiterhin kein eigener Traffic aus dieser Quelle."
        else:
            outcome, detail = "neutral", f"Eigene Views aus dieser Quelle {baseline} → {current}."
        row.status, row.outcome, row.evaluated_at = "evaluated", outcome, now
        row.evaluation = {"baseline_views": baseline, "current_views": current, "detail": detail, "window_days": MEMORY_DAYS}
        evaluated += 1
    return evaluated


# ----------------------------------------------------------------------------- read models
def best_for_video(session, video_id, day=None):
    """Best open opportunity (today or the latest snapshot day) matched to one own video."""
    latest = day or session.scalar(select(func.max(DiscoveryOpportunity.day)))
    if latest is None:
        return None
    rows = list(session.scalars(select(DiscoveryOpportunity).where(DiscoveryOpportunity.day == latest, DiscoveryOpportunity.video_id == video_id,
                                                                     DiscoveryOpportunity.gap != "insufficient_evidence")))
    if not rows:
        return None
    best = max(rows, key=lambda r: (bool(r.evidence.get("actionable")), bool(r.evidence.get("context_usable")),
                                    r.scores.get("external_audience_score") or 0))
    return {"kind": best.kind, "key": best.key, "gap": best.gap, "scores": best.scores, "evidence": best.evidence, "day": str(best.day),
            "score": best.scores.get("external_audience_score"), "demand_source": best.evidence.get("demand_source"),
            "evidence_level": best.evidence.get("evidence_level"), "actionable": bool(best.evidence.get("actionable")),
            "families": best.evidence.get("families") or [], "family_labels": best.evidence.get("family_labels") or [],
            "context_usable": bool(best.evidence.get("context_usable")), "context_reason": best.evidence.get("context_reason"),
            "missing_evidence": best.evidence.get("missing_evidence") or [], "uncertainty": best.evidence.get("uncertainty"),
            "seed_quality": best.evidence.get("seed_quality"), "members": (best.evidence.get("members") or [])[:5],
            "channel": best.evidence.get("channel"), "own_source_views_90d": best.evidence.get("own_source_views_90d") or {},
            "audience": best.evidence.get("title") or best.key, "shared_tokens": best.evidence.get("shared_tokens") or best.evidence.get("missing_title_tokens")}


def demand_evidence(session, video_id, terms):
    """YouTube suppresses search-term detail below a threshold: say so instead of falling back to proxies silently."""
    if terms:
        return {"status": "available", "note": None}
    last = session.scalar(select(func.max(TrafficDaily.day)).where(TrafficDaily.video_id == video_id))
    search_views = 0
    if last is not None:
        search_views = session.scalar(select(func.coalesce(func.sum(TrafficDaily.views), 0)).where(
            TrafficDaily.video_id == video_id, TrafficDaily.source == "YT_SEARCH",
            TrafficDaily.day >= last-timedelta(days=SIGNAL_WINDOW_DAYS-1))) or 0
    if search_views:
        return {"status": "unavailable_below_api_threshold", "search_views_90d": int(search_views),
                "note": f"{int(search_views)} Search-Views in 90 Tagen, aber YouTube liefert keine Suchbegriff-Details "
                        "(Aggregationsschwelle): keine reale Nachfrage-Evidenz verfügbar."}
    return {"status": "no_search_traffic", "search_views_90d": 0, "note": "Keine Search-Views im Fenster."}


def overview(session, now=None):
    now = now or utcnow()
    latest = session.scalar(select(func.max(DiscoveryOpportunity.day)))
    run_row = session.scalar(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()))
    quota = session.get(DiscoveryQuota, pacific_day(now))
    videos = {v.id: v.title for v in session.scalars(select(Video).where(Video.active.is_(True)))}
    opportunities = []
    if latest:
        for r in session.scalars(select(DiscoveryOpportunity).where(DiscoveryOpportunity.day == latest)):
            history = [{"day": str(h.day), "score": h.scores.get("external_audience_score")} for h in session.scalars(
                select(DiscoveryOpportunity).where(DiscoveryOpportunity.kind == r.kind, DiscoveryOpportunity.key == r.key).order_by(DiscoveryOpportunity.day.desc()).limit(6))][::-1]
            opportunities.append({"kind": r.kind, "key": r.key, "gap": r.gap, "video_id": r.video_id, "video_title": videos.get(r.video_id),
                "scores": r.scores, "evidence": r.evidence, "components": r.components, "status": r.status, "outcome": r.outcome, "trend": history})
    opportunities.sort(key=lambda o: -(o["scores"].get("external_audience_score") or 0))
    usable = [o for o in opportunities if o["gap"] != "insufficient_evidence"]
    actionable = [o for o in usable if o["evidence"].get("actionable")]
    per_video = {vid: best_for_video(session, vid, latest) for vid in videos} if latest else {vid: None for vid in videos}
    _, level_weights, record = memory_weights(session)
    signals = {}
    for vid in videos:
        terms = list(session.execute(select(DiscoverySignal.detail, DiscoverySignal.views).where(DiscoverySignal.video_id == vid, DiscoverySignal.kind == "own_search_term",
            DiscoverySignal.window_end == session.scalar(select(func.max(DiscoverySignal.window_end)).where(DiscoverySignal.video_id == vid))).order_by(DiscoverySignal.views.desc()).limit(8)))
        sources = list(session.execute(select(DiscoverySignal.detail, DiscoverySignal.views).where(DiscoverySignal.video_id == vid, DiscoverySignal.kind == "own_suggested_source",
            DiscoverySignal.window_end == session.scalar(select(func.max(DiscoverySignal.window_end)).where(DiscoverySignal.video_id == vid))).order_by(DiscoverySignal.views.desc()).limit(8)))
        signals[vid] = {"search_terms": [{"term": t, "views": v} for t, v in terms],
                        "suggested_sources": [{"video_id": t, "views": v} for t, v in sources],
                        "demand_evidence": demand_evidence(session, vid, terms)}
    return {"version": VERSION, "day": str(latest) if latest else None, "best": (actionable or usable or [None])[0],
            "top": usable[:12], "insufficient": len(opportunities)-len(usable), "actionable": len(actionable),
            "evidence_policy": {"levels": EVIDENCE_LEVELS, "actionable_levels": ACTIONABLE_LEVELS, "caps": LEVEL_CAPS,
                                "families": FAMILIES, "min_proxy_families": MIN_PROXY_FAMILIES,
                                "proxy_score_cap": PROXY_SCORE_CAP, "min_seed_tokens": MIN_SEED_TOKENS,
                                "min_relevance": MIN_RELEVANCE, "level_weights": level_weights,
                                "note": "Eigene Analytics-Nachfrage ist die stärkste Evidenz. Fehlt sie, dürfen mindestens "
                                        f"{MIN_PROXY_FAMILIES} unabhängige Evidenzfamilien gemeinsam ein Experiment tragen "
                                        f"(gedeckelt bei {MULTI_PROXY_SCORE_CAP}); ein einzelner Proxy bleibt Hypothese "
                                        f"(gedeckelt bei {PROXY_SCORE_CAP}) und löst nie eine Maßnahme aus. Kein Suchvolumen, "
                                        "keine erfundenen Nachfragewerte; Ein-Wort-Seeds und aus dem eigenen Titel "
                                        "zurückgewonnene Begriffe zählen nicht als Nachfrage."},
            "per_video": per_video, "signals": signals,
            "clusters": [o for o in opportunities if o["kind"] == "cluster"][:8],
            "last_run": {"day": str(run_row.day), "status": run_row.status, "units_used": run_row.units_used, "issues": run_row.issues, "stats": run_row.stats,
                         "finished_at": run_row.finished_at} if run_row else None,
            "quota": {"day": str(pacific_day(now)), "units_used": quota.units if quota else 0, "daily_limit": DAILY_UNITS, "search_cost": SEARCH_COST},
            "memory": record, "capabilities": CAPABILITIES, "gaps": GAPS,
            "limits": [f"Proxy-only Chancen sind bei {PROXY_SCORE_CAP} gedeckelt und lösen keine aktive Maßnahme aus.",
                       "Die Seed-Relevanz ist zirkulär (Seeds stammen aus eigenem Titel/Tags) und wird nicht als Evidenz gewertet.",
                       "Externe öffentliche Daten erhöhen n_videos nicht; interne Generalisierung bleibt low/insufficient.",
                       "Nachfrage aus Such-Proben ist ein Proxy (Ergebnis-Reichweite), kein Suchvolumen.",
                       "Fremde Erfolge werden nicht kausal erklärt und nicht kopiert; Nachbarvideos sind Audience-Signale.",
                       "Werbephasen liefern keine organische Nachfrage-Evidenz."], "read_only": True}
