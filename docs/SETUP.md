# Google-Zugang und lokaler Betrieb

Für den neuen Betrieb unabhängig vom PC gilt [VERCEL.md](VERCEL.md). Die folgenden Dateipfade und Docker-Befehle beschreiben ausschließlich die lokale Alternative.

## Google Cloud

1. Ein eigenes Google-Cloud-Projekt anlegen.
2. **YouTube Data API v3**, **YouTube Analytics API** und fÃ¼r Thumbnail-Reichweite **YouTube Reporting API** aktivieren.
3. OAuth-Zustimmungsbildschirm konfigurieren. Beim Testbetrieb dein Google-Konto als Testnutzer hinterlegen.
4. OAuth-Client vom Typ **Desktop-App** erstellen. Die heruntergeladene JSON-Datei ausschlieÃŸlich lokal unter `secrets/client_secret.json` speichern.
5. `python -m app.cli oauth` ausfÃ¼hren. Der lokale Loopback-Dialog Ã¼bernimmt State/PKCE Ã¼ber die Google-Bibliothek und speichert den Refresh-Token in `secrets/token.json`.
6. Richtigen Kanal autorisieren; `CHANNEL_ID` dient zusÃ¤tzlich als Schutz vor versehentlichem Import eines anderen Kanals.

Scopes:
- `https://www.googleapis.com/auth/youtube.readonly`
- `https://www.googleapis.com/auth/yt-analytics.readonly`
- Nur mit `ENABLE_REVENUE=true`: `https://www.googleapis.com/auth/yt-analytics-monetary.readonly`.

Nach einer Scope-Erweiterung OAuth erneut ausfÃ¼hren. Service Accounts ersetzen diese Kanalinhaber-Autorisierung nicht. Ein API-Key wird fÃ¼r diese Implementierung nicht benÃ¶tigt.

Google kann Test-Autorisierungen zeitlich begrenzen oder bei Ã„nderungen widerrufen. Bei `RefreshError` erneut autorisieren; bei `HttpError` API-Aktivierung, Kanalberechtigung und Google-Quota prÃ¼fen. Rohantworten mit mÃ¶glicherweise sensiblen Angaben werden nicht ins Dashboard geschrieben.

## Konfiguration

`.env.example` enthÃ¤lt ausschlieÃŸlich Platzhalter. Nach dem Kopieren mÃ¼ssen **alle** ersetzt werden. Als Betriebswerte sind `ENABLE_REVENUE=false`, `ENABLE_REACH=true`, `SYNC_INTERVAL_SECONDS=3600` und `ANALYTICS_LAG_DAYS=3` vorgesehen. Die lokalen Standardpfade sind `secrets/client_secret.json` und `secrets/token.json`. FÃ¼r Compose lautet das Verbindungsformat `postgresql+psycopg://growth:<DEIN_PASSWORT>@db:5432/growth`. Zugangswerte nicht aus Beispielen oder Tests Ã¼bernehmen.


| Variable | Bedeutung |
|---|---|
| DATABASE_URL | PostgreSQL-Verbindung; im Compose-Netz Host `db` |
| POSTGRES_PASSWORD | Passwort des Compose-PostgreSQL-Nutzers |
| APP_TOKEN | Eigenes langes zufÃ¤lliges Dashboard-Token |
| CHANNEL_ID | Erwartete Kanal-ID; empfohlen verpflichtend selbst setzen |
| FOCUS_VIDEO_ID | Bestehendes Video mit etwa 20.000 Views |
| GOOGLE_CLIENT_FILE | Pfad zur OAuth-Clientdatei |
| GOOGLE_TOKEN_FILE | Pfad zur lokalen Token-Datei |
| ENABLE_REVENUE | Standard false; nur bei passenden Rechten aktivieren |
| ENABLE_REACH | Standard true; Reporting-Job fÃ¼r Thumbnail-Reichweite |
| SYNC_INTERVAL_SECONDS | Standard 3600; Minimum des Workers 300 |
| ANALYTICS_LAG_DAYS | Standard 3; mindestens 2 Tage Sicherheitsabstand |

API und Worker sind getrennte Prozesse. PostgreSQL-Advisory-Lock verhindert parallele ImportlÃ¤ufe. Bei fehlgeschlagenem Teilbericht lÃ¤uft der Rest weiter; der Import wird als `partial` angezeigt. Der Reporting-Job wird beim ersten Abruf angelegt. Das ist ein Berichtsauftrag, keine VerÃ¶ffentlichung am Kanal.

## Secrets

`.env`, Token-Dateien und Clientdateien sind in Git und im Docker-Build-Kontext ausgeschlossen. Der API-Container erhÃ¤lt keinen Token-Datei-Mount; nur der Worker liest die lokalen Google-Dateien. Unter Linux benÃ¶tigt Container-UID 10001 Leserechte auf diese Dateien; restriktive Gruppenrechte/ACL passend setzen. Nicht weltlesbar machen. Der Browser behÃ¤lt das Dashboard-Token nur im Arbeitsspeicher; nach Neuladen erneut anmelden.

Der OAuth-Refresh lÃ¤uft wÃ¤hrend API-Aufrufen im Arbeitsspeicher. Die Token-Datei wird vom Worker nicht Ã¼berschrieben. Eine erneute interaktive Autorisierung erfolgt nur Ã¼ber den lokalen CLI-Befehl.

## Betrieb

- Docker-Dienst vor `docker compose --profile local-worker up --build -d` starten.
- Dashboard auf `127.0.0.1:8000`; keine Ã¶ffentliche VerÃ¶ffentlichung eingerichtet.
- `docker compose logs worker`: Importstatus und gekÃ¼rzte Fehlerklassen.
- `/health` prÃ¼ft die Datenbankverbindung; authentifiziertes `/api/dashboard` enthÃ¤lt Importprobleme und Zeitstempel.
- Geplante PostgreSQL-Backups vor produktivem Dauerbetrieb einrichten und Wiederherstellung testen.
- Single-Owner-V1: FÃ¼r mehrere Personen spÃ¤ter OIDC, Rollen, Audit-IdentitÃ¤ten und TLS-Reverse-Proxy ergÃ¤nzen.
- Vor Langzeitbetrieb Datenaufbewahrung, Widerruf/LÃ¶schworkflow und aktuelle YouTube-Entwicklerbedingungen fÃ¼r den konkreten Einsatz prÃ¼fen. V1 lÃ¶scht keine Kanaldaten und fÃ¼hrt keine KanalÃ¤nderungen aus.
- Tests/Debugging mit neuer leerer Datenbank ausfÃ¼hren. `alembic downgrade` ist ausschlieÃŸlich fÃ¼r isolierte Testdatenbanken gedacht.

## Offizielle Referenzen

- [Desktop-OAuth](https://developers.google.com/youtube/v3/guides/auth/installed-apps)
- [Analytics-Berichte und Berechtigungen](https://developers.google.com/youtube/analytics/channel_reports)
- [Metrikdefinitionen](https://developers.google.com/youtube/analytics/metrics)
- [Reporting-Reichweitenberichte](https://developers.google.com/youtube/reporting/v1/reports/channel_reports)
- [Reports Query / DatenverfÃ¼gbarkeit](https://developers.google.com/youtube/analytics/reference/reports/query)

GeprÃ¼ft am 11.09.2026. MaÃŸgeblich ist immer der tatsÃ¤chlich vom autorisierten Kanal verfÃ¼gbare Bericht.
