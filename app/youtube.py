"""Official Google clients. No scraping and no channel write permissions."""
import csv
import io
import json
import httplib2
from google_auth_httplib2 import AuthorizedHttp
from pathlib import Path
from urllib.parse import urlparse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build
from .config import settings
from .budget import Budget

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def scopes():
    return SCOPES + (["https://www.googleapis.com/auth/yt-analytics-monetary.readonly"] if settings.enable_revenue else [])


def authorize():
    if settings.hosted:
        raise ValueError("Interactive OAuth is local only; use Vercel environment variables.")
    flow = InstalledAppFlow.from_client_secrets_file(settings.google_client_file, scopes())
    credentials = flow.run_local_server(host="127.0.0.1", port=0, access_type="offline", prompt="consent")
    target = Path(settings.google_token_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(credentials.to_json(), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print("OAuth token saved locally. Do not commit or share it.")


class YouTube:
    def __init__(self, budget=None):
        self.budget = budget
        values = (settings.google_client_id, settings.google_client_secret, settings.google_refresh_token)
        if any(values):
            if not all(values) or any(v.startswith("REPLACE_") for v in values):
                raise ValueError("All three Google OAuth environment variables are required.")
            self.credentials = Credentials(token=None, refresh_token=settings.google_refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.google_client_id, client_secret=settings.google_client_secret,
                scopes=scopes())
        elif settings.hosted:
            raise ValueError("Google OAuth environment variables are required; secret files are forbidden on Vercel.")
        else:
            self.credentials = Credentials.from_authorized_user_file(settings.google_token_file, scopes())
        self.data = build("youtube", "v3", http=AuthorizedHttp(self.credentials, http=httplib2.Http(timeout=15)), cache_discovery=False)
        self.analytics = build("youtubeAnalytics", "v2", http=AuthorizedHttp(self.credentials, http=httplib2.Http(timeout=15)), cache_discovery=False)
        self.reporting = build("youtubereporting", "v1", http=AuthorizedHttp(self.credentials, http=httplib2.Http(timeout=15)), cache_discovery=False)

    def execute(self, request, http=None):
        if getattr(self, "budget", None):
            self.budget.check()
        # No unbounded retry/backoff inside the limited function invocation.
        # The next cron resumes failed chunks from database checkpoints.
        return request.execute(num_retries=0, http=http) if http is not None else request.execute(num_retries=0)

    def channel(self):
        items = self.execute(self.data.channels().list(part="snippet,statistics,contentDetails", mine=True))["items"]
        matches = [item for item in items if not settings.channel_id or item["id"] == settings.channel_id]
        if len(matches) != 1:
            raise ValueError("OAuth channel does not match CHANNEL_ID; authorize the correct channel.")
        return matches[0]

    def videos(self, playlist):
        page = None
        while True:
            response = self.execute(self.data.playlistItems().list(
                part="contentDetails", playlistId=playlist, maxResults=50, pageToken=page))
            ids = [row["contentDetails"]["videoId"] for row in response.get("items", [])]
            if ids:
                yield from self.execute(self.data.videos().list(
                    part="snippet,contentDetails,statistics", id=",".join(ids)))["items"]
            page = response.get("nextPageToken")
            if not page:
                break

    def query(self, video, start, end, metrics, dimensions=None, filters=None, timeout=None, max_pages=None):
        args = dict(ids="channel==MINE", startDate=str(start), endDate=str(end),
                    metrics=metrics, filters=f"video=={video}" + (f";{filters}" if filters else ""))
        if dimensions:
            args["dimensions"] = dimensions
        rows = []
        offset = 1
        pages = 0
        transport = AuthorizedHttp(self.credentials, http=httplib2.Http(timeout=timeout), max_refresh_attempts=0) if timeout else None
        while True:
            pages += 1
            if max_pages is not None and pages > max_pages:
                raise TimeoutError("Optional report page budget exceeded")
            request = self.analytics.reports().query(**args, startIndex=offset, maxResults=200)
            result = self.execute(request, http=transport) if transport else self.execute(request)
            headers = [h["name"] for h in result.get("columnHeaders", [])]
            batch = result.get("rows", [])
            rows.extend(dict(zip(headers, r)) for r in batch)
            if len(batch) < 200:
                return rows
            offset += len(batch)

    def traffic_detail(self, video, start, end, source_type, max_results=25):
        """Top search terms / recommending videos / external URLs behind one traffic source (own analytics, no Data API quota)."""
        request = self.analytics.reports().query(ids="channel==MINE", startDate=str(start), endDate=str(end),
            metrics="views,estimatedMinutesWatched", dimensions="insightTrafficSourceDetail",
            filters=f"video=={video};insightTrafficSourceType=={source_type}", sort="-views", maxResults=min(25, max_results))
        result = self.execute(request)
        headers = [h["name"] for h in result.get("columnHeaders", [])]
        return [dict(zip(headers, r)) for r in result.get("rows", [])]

    # Public read-only Data API lookups for discovery. Quota: search.list 100 units, list calls 1 unit.
    def search(self, query, max_results=25, published_after=None, order="relevance"):
        args = dict(part="snippet", q=query, type="video", maxResults=min(50, max_results), order=order, safeSearch="none")
        if published_after:
            args["publishedAfter"] = published_after
        result = self.execute(self.data.search().list(**args))
        return [{"video_id": item["id"]["videoId"], "channel_id": item["snippet"]["channelId"], "title": item["snippet"]["title"],
                 "channel_title": item["snippet"].get("channelTitle", ""), "published_at": item["snippet"]["publishedAt"]}
                for item in result.get("items", []) if item.get("id", {}).get("videoId")]

    def videos_by_id(self, ids):
        rows = []
        ids = list(dict.fromkeys(ids))
        for offset in range(0, len(ids), 50):
            result = self.execute(self.data.videos().list(part="snippet,contentDetails,statistics", id=",".join(ids[offset:offset+50])))
            rows.extend(result.get("items", []))
        return rows

    def own_playlists(self, max_pages=4):
        """Die eigenen Playlists (read-only, 1 Einheit je Seite). Ohne diese Liste ist ihre Existenz unbekannt,
        und ein Experiment darf keine Playlist voraussetzen, die es vielleicht gar nicht gibt."""
        rows, token, pages = [], None, 0
        while pages < max_pages:
            result = self.execute(self.data.playlists().list(part="snippet,contentDetails,status", mine=True,
                                                             maxResults=50, pageToken=token))
            rows.extend(result.get("items", []))
            token = result.get("nextPageToken")
            pages += 1
            if not token:
                break
        return rows

    def channels_by_id(self, ids):
        rows = []
        ids = list(dict.fromkeys(ids))
        for offset in range(0, len(ids), 50):
            result = self.execute(self.data.channels().list(part="snippet,statistics", id=",".join(ids[offset:offset+50])))
            rows.extend(result.get("items", []))
        return rows

    def reach_reports(self):
        """Discover current reach report type instead of hardcoding a retired ID."""
        types = self.execute(self.reporting.reportTypes().list()).get("reportTypes", [])
        available = [r for r in types if "reach_basic" in r["id"] and not r.get("systemManaged")]
        if not available:
            raise ValueError("No reach report type available for this channel.")
        report_type = sorted(available, key=lambda r: r["id"])[-1]["id"]
        jobs, page = [], None
        while True:
            result = self.execute(self.reporting.jobs().list(pageToken=page))
            jobs.extend(result.get("jobs", []))
            page = result.get("nextPageToken")
            if not page:
                break
        job = next((j for j in jobs if j["reportTypeId"] == report_type), None)
        if not job:
            job = self.execute(self.reporting.jobs().create(body={
                "reportTypeId": report_type, "name": "Growth V1 reach"}))
        reports, page = [], None
        while True:
            result = self.execute(self.reporting.jobs().reports().list(jobId=job["id"], pageToken=page))
            reports.extend(result.get("reports", []))
            page = result.get("nextPageToken")
            if not page:
                break
        # Later generated replacements must overwrite earlier reports.
        for report in sorted(reports, key=lambda r: r.get("createTime", "")):
            yield report

    def download_reach(self, url):
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme != "https" or not (host == "googleapis.com" or host.endswith(".googleapis.com")):
            raise ValueError("Unexpected Reporting download host.")
        if getattr(self, "budget", None):
            self.budget.check()
        response = AuthorizedSession(self.credentials).get(url, timeout=15)
        response.raise_for_status()
        return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
