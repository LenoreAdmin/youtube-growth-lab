# Google-Zugang und Betrieb

## Google Cloud

1. Ein eigenes Google-Cloud-Projekt anlegen.
2. **YouTube Data API v3**, **YouTube Analytics API** und für Thumbnail-Reichweite **YouTube Reporting API** aktivieren.
3. OAuth-Zustimmungsbildschirm konfigurieren. Beim Testbetrieb dein Google-Konto als Testnutzer hinterlegen.
4. OAuth-Client vom Typ **Desktop-App** erstellen. Die heruntergeladene JSON-Datei ausschließlich lokal unter `secrets/client_secret.json` speichern.
5. `python -m app.cli oauth` ausführen. Der lokale Loopback-Dialog übernimmt State/PKCE über die Google-Bibliothek und speichert den Refresh-Token in `secrets/token.json`.
6. Richtigen Kanal autorisieren; `CHANNEL_ID` dient zusätzlich als Schutz vor versehentlichem Import eines anderen Kanals.

Scopes:
- `https://www.googleapis.com/auth/youtube.readonly`
- `https://www.googleapis.com/auth/yt-analytics.readonly`
- Nur mit `ENABLE_REVENUE=true`: `https://www.googleapis.com/auth/yt-analytics-monetary.readonly`.

Nach einer Scope-Erweiterung OAuth erneut ausführen. Service Accounts ersetzen diese Kanalinhaber-Autorisierung nicht. Ein API-Key wird für diese Implementierung nicht benötigt.

Google kann Test-Autorisierungen zeitlich begrenzen oder bei Änderungen widerrufen. Bei `RefreshError` erneut autorisieren; bei `HttpError` API-Aktivierung, Kanalberechtigung und Google-Quota prüfen. Rohantworten mit möglicherweise sensiblen Angaben werden nicht ins Dashboard geschrieben.

## Konfiguration

`.env.example` enthält ausschließlich Platzhalter. Nach dem Kopieren müssen **alle** ersetzt werden. Als Betriebswerte sind `ENABLE_REVENUE=false`, `ENABLE_REACH=true`, `SYNC_INTERVAL_SECONDS=3600` und `ANALYTICS_LAG_DAYS=3` vorgesehen. Die lokalen Standardpfade sind `secrets/client_secret.json` und `secrets/token.json`. Für Compose lautet das Verbindungsformat `postgresql+psycopg://growth:<DEIN_PASSWORT>@db:5432/growth`. Zugangswerte nicht aus Beispielen oder Tests übernehmen.


| Variable | Bedeutung |
|---|---|
| DATABASE_URL | PostgreSQL-Verbindung; im Compose-Netz Host `db` |
| POSTGRES_PASSWORD | Passwort des Compose-PostgreSQL-Nutzers |
| APP_TOKEN | Eigenes langes zufälliges Dashboard-Token |
| CHANNEL_ID | Erwartete Kanal-ID; empfohlen verpflichtend selbst setzen |
| FOCUS_VIDEO_ID | Bestehendes Video mit etwa 20.000 Views |
| GOOGLE_CLIENT_FILE | Pfad zur OAuth-Clientdatei |
| GOOGLE_TOKEN_FILE | Pfad zur lokalen Token-Datei |
| ENABLE_REVENUE | Standard false; nur bei passenden Rechten aktivieren |
| ENABLE_REACH | Standard true; Reporting-Job für Thumbnail-Reichweite |
| SYNC_INTERVAL_SECONDS | Standard 3600; Minimum des Workers 300 |
| ANALYTICS_LAG_DAYS | Standard 3; mindestens 2 Tage Sicherheitsabstand |

API und Worker sind getrennte Prozesse. PostgreSQL-Advisory-Lock verhindert parallele Importläufe. Bei fehlgeschlagenem Teilbericht läuft der Rest weiter; der Import wird als `partial` angezeigt. Der Reporting-Job wird beim ersten Abruf angelegt. Das ist ein Berichtsauftrag, keine Veröffentlichung am Kanal.

## Secrets

`.env`, Token-Dateien und Clientdateien sind in Git und im Docker-Build-Kontext ausgeschlossen. Der API-Container erhält keinen Token-Datei-Mount; nur der Worker liest die lokalen Google-Dateien. Unter Linux benötigt Container-UID 10001 Leserechte auf diese Dateien; restriktive Gruppenrechte/ACL passend setzen. Nicht weltlesbar machen. Der Browser behält das Dashboard-Token nur im Arbeitsspeicher; nach Neuladen erneut anmelden.

Der OAuth-Refresh läuft während API-Aufrufen im Arbeitsspeicher. Die Token-Datei wird vom Worker nicht überschrieben. Eine erneute interaktive Autorisierung erfolgt nur über den lokalen CLI-Befehl.

## Betrieb

- Docker-Dienst vor `docker compose up --build -d` starten.
- Dashboard auf `127.0.0.1:8000`; keine öffentliche Veröffentlichung eingerichtet.
- `docker compose logs worker`: Importstatus und gekürzte Fehlerklassen.
- `/health` prüft die Datenbankverbindung; authentifiziertes `/api/dashboard` enthält Importprobleme und Zeitstempel.
- Geplante PostgreSQL-Backups vor produktivem Dauerbetrieb einrichten und Wiederherstellung testen.
- Single-Owner-V1: Für mehrere Personen später OIDC, Rollen, Audit-Identitäten und TLS-Reverse-Proxy ergänzen.
- Vor Langzeitbetrieb Datenaufbewahrung, Widerruf/Löschworkflow und aktuelle YouTube-Entwicklerbedingungen für den konkreten Einsatz prüfen. V1 löscht keine Kanaldaten und führt keine Kanaländerungen aus.
- Tests/Debugging mit neuer leerer Datenbank ausführen. `alembic downgrade` ist ausschließlich für isolierte Testdatenbanken gedacht.

## Offizielle Referenzen

- [Desktop-OAuth](https://developers.google.com/youtube/v3/guides/auth/installed-apps)
- [Analytics-Berichte und Berechtigungen](https://developers.google.com/youtube/analytics/channel_reports)
- [Metrikdefinitionen](https://developers.google.com/youtube/analytics/metrics)
- [Reporting-Reichweitenberichte](https://developers.google.com/youtube/reporting/v1/reports/channel_reports)
- [Reports Query / Datenverfügbarkeit](https://developers.google.com/youtube/analytics/reference/reports/query)

Geprüft am 11.09.2026. Maßgeblich ist immer der tatsächlich vom autorisierten Kanal verfügbare Bericht.
