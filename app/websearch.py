"""Public web search for audience pools outside our own reach – free tier, hard capped, read-only.

Provider: Google Programmable Search Engine with the Custom Search JSON API. Chosen because its free
tier needs no payment method and runs in the Google Cloud project this channel already uses for the
YouTube API. Brave now asks for a card even on its free plan and Bing's free tier was retired.

Three rules keep this honest:
- a hard daily cap well below the free quota, counted in the database, so no run can ever cost money,
- nothing is fetched or posted on our behalf: the API returns titles, URLs and snippets, and a page is
  only checked for reachability,
- a result is a candidate, not a finding. Relevance is decided afterwards from shared specific terms,
  community markers and reachability; everything else is discarded.
"""
import logging
import re
from datetime import date
from sqlalchemy import select
from .backfill import upsert
from .config import settings
from .models import WebSearchQuota, utcnow

log = logging.getLogger(__name__)
ENDPOINT = "https://www.googleapis.com/customsearch/v1"
PROVIDERS = ("google_cse",)
SETUP = [
    "Google Cloud Console (dasselbe Projekt wie die YouTube-API): APIs & Dienste → Bibliothek → "
    "„Custom Search API“ aktivieren.",
    "APIs & Dienste → Anmeldedaten → API-Schlüssel erstellen (optional auf die Custom Search API beschränken).",
    "https://programmablesearchengine.google.com/controlpanel/create → „Das gesamte Web durchsuchen“ "
    "aktivieren → Suchmaschinen-ID (cx) kopieren.",
    "Vercel → Projekt → Settings → Environment Variables (Production): WEB_SEARCH_PROVIDER=google_cse, "
    "WEB_SEARCH_KEY=<API-Schlüssel>, WEB_SEARCH_CX=<cx>.",
]


class SearchUnavailable(Exception):
    """No provider configured, cap reached or upstream refused – never a reason to fail a whole run."""


def configured():
    return (settings.web_search_provider in PROVIDERS and bool(settings.web_search_key)
            and bool(settings.web_search_cx))


def status():
    if not settings.web_search_provider:
        return {"configured": False, "provider": None, "reason": "WEB_SEARCH_PROVIDER ist nicht gesetzt.",
                "setup": SETUP, "daily_limit": settings.web_search_daily_limit}
    if not configured():
        missing = [name for name, value in (("WEB_SEARCH_KEY", settings.web_search_key),
                                            ("WEB_SEARCH_CX", settings.web_search_cx)) if not value]
        return {"configured": False, "provider": settings.web_search_provider,
                "reason": f"Fehlende Konfiguration: {', '.join(missing) or 'unbekannter Provider'}.",
                "setup": SETUP, "daily_limit": settings.web_search_daily_limit}
    return {"configured": True, "provider": settings.web_search_provider, "reason": None,
            "daily_limit": settings.web_search_daily_limit,
            "note": ("Kostenloses Kontingent des Providers: 100 Abfragen/Tag. Dieses System hält sich an ein "
                     f"eigenes Limit von {settings.web_search_daily_limit} und zählt jede Abfrage mit.")}


def used_today(session, day=None):
    row = session.get(WebSearchQuota, day or date.today())
    return row.queries if row else 0


def _count(session, day, amount=1):
    statement = upsert(session, WebSearchQuota).values(day=day, queries=amount, updated_at=utcnow())
    session.execute(statement.on_conflict_do_update(index_elements=["day"], set_={
        "queries": WebSearchQuota.queries+amount, "updated_at": statement.excluded.updated_at}))
    session.commit()


def remaining(session, day):
    return max(0, settings.web_search_daily_limit-used_today(session, day))


def search(session, query, day, count=10, client=None):
    """One public search. Raises SearchUnavailable instead of letting a run fail."""
    if not configured():
        raise SearchUnavailable("kein Such-Provider konfiguriert")
    if remaining(session, day) <= 0:
        raise SearchUnavailable(f"eigenes Tageslimit von {settings.web_search_daily_limit} Abfragen erreicht")
    params = {"key": settings.web_search_key, "cx": settings.web_search_cx, "q": query,
              "num": max(1, min(10, count)), "safe": "active"}
    try:
        import httpx
        opener = client or httpx.Client(timeout=8.0, headers={"User-Agent": "youtube-growth-lab/1.0"})
        try:
            response = opener.get(ENDPOINT, params=params)
        finally:
            if client is None:
                opener.close()
    except Exception as exc:
        raise SearchUnavailable(f"Netzfehler: {type(exc).__name__}") from None
    _count(session, day)
    if response.status_code == 429:
        raise SearchUnavailable("Provider-Kontingent erschöpft (429)")
    if response.status_code >= 400:
        raise SearchUnavailable(f"Provider antwortete mit HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception:
        raise SearchUnavailable("unlesbare Antwort") from None
    results = []
    for item in payload.get("items") or []:
        link = item.get("link") or ""
        if not link.startswith(("http://", "https://")):
            continue
        results.append({"title": (item.get("title") or "")[:300], "url": link[:500],
                        "snippet": re.sub(r"\s+", " ", item.get("snippet") or "")[:500],
                        "query": query})
    return results
