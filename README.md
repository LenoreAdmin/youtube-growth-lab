# YouTube Growth Lab · V1

Ein lokal betreibbares System für echte YouTube-Daten, Wachstumssignale, Prognosen und ein lernendes Experiment Memory. Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy/Alembic und scikit-learn. Die Oberfläche ist deutschsprachig und wird direkt von FastAPI ausgeliefert.

**Status:** ausführbare V1 mit getesteter Import- und Lernlogik. Ohne deinen OAuth-Zugang wurden keine echten Kanaldaten abgerufen. Die Demo ist vollständig synthetisch. Keine Videos, Titel, Thumbnails, Kommentare oder sonstigen Kanalinhalte werden veröffentlicht oder geändert.

## Start mit echten Daten

1. Python 3.12 und Docker Desktop mit laufendem Docker-Dienst bereitstellen.
2. Im Projektverzeichnis:
   ```powershell
   python -m venv .venv
   .venv/Scripts/python.exe -m pip install --no-cache-dir -c constraints.txt ".[dev]"
   Copy-Item .env.example .env
   ```
3. **Alle** `REPLACE_WITH_...`-Werte in `.env` ersetzen, einschließlich Pfaden, booleschen Schaltern und Zahlen. Empfohlene Werte stehen in [SETUP.md](docs/SETUP.md). Die Vorlage enthält bewusst keine betriebsfertigen Werte. Anschließend ein eigenes `POSTGRES_PASSWORD` und die identische URL-kodierte Passwortkomponente in `DATABASE_URL` setzen. Ein zufälliges alphanumerisches Passwort vermeidet URL-Sonderzeichen. `APP_TOKEN` unabhängig davon erzeugen:
   ```powershell
   .venv/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Diesen Wert nur in der lokalen `.env` ablegen. `CHANNEL_ID` auf den gewünschten Kanal und `FOCUS_VIDEO_ID` auf das vorhandene Video mit ca. 20.000 Views setzen.
4. Google-Zugang gemäß [SETUP.md](docs/SETUP.md) einrichten; OAuth-Clientdatei nach `secrets/client_secret.json` legen.
   ```powershell
   .venv/Scripts/python.exe -m app.cli oauth
   ```
5. PostgreSQL, Migration, API und Worker starten:
   ```powershell
   docker compose up --build -d
   docker compose logs --tail 80 worker
   ```
6. [Dashboard öffnen](http://127.0.0.1:8000), mit deinem `APP_TOKEN` anmelden. Der Worker synchronisiert standardmäßig stündlich. Erst nach genügend zeitlich getrennten Snapshots entstehen aktuelle Wachstumsmetriken.

Der OAuth-Dialog muss mit dem richtigen Kanalinhaber/Brand-Konto abgeschlossen werden. Ein API-Key allein reicht nicht. Keine Secrets in Chat, Git oder Tickets kopieren.

## Ohne Google-Zugang testen

Separate Datenbank verwenden. Niemals Demo und echte Daten mischen:
```powershell
$env:DATABASE_URL = "sqlite:///demo.db"
$env:APP_TOKEN = "local-demo-only"
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m app.cli demo
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

SQLite dient ausschließlich Tests und der lokalen Demo; der dauerhafte Betrieb verwendet PostgreSQL. Der Demo-Importer verweigert eine bereits befüllte Datenbank; die echte Pipeline verweigert einen Kanalwechsel in derselben Datenbank. Demo-Variablen in einer neuen Shell nicht übernehmen.

## Enthalten

- Offizielle Data-, Analytics- und ergänzende Reporting-API-Adapter mit OAuth, Pagination, Zeitlimits und Retries.
- Upload-Metadaten, kumulative Snapshots, historische Tagesdaten, Audience Retention, Traffic-Quellen, Reichweite/CTR und optional Umsatz.
- Inkrementelle Importe mit 30 Tagen Überlappung für Datenkorrekturen, eindeutigen Schlüsseln und Importstatus.
- View Velocity, Beschleunigung, Conversion, Watchtime-Effizienz, vergleichbare Kanal-Kohorten und erklärter Momentum Score.
- Gespeicherte Prognosen für 24 Stunden, 7 Tage und 30 Tage einschließlich späterem Soll-Ist-Vergleich.
- Adaptiver Basisansatz, zeitlich validierter Ridge-Challenger und empirische Unsicherheitsbereiche.
- Experiment Memory mit unveränderlicher Vorregistrierung, Kontrollvideo, Ergebnissen, Fehlern, Confidence, Interpretationshistorie und nächster Hypothese.
- Separate Monetarisierung; vorgeschlagene Bandit-Experimente und Schnittstellen für XGBoost/Bayesian Optimization, ohne automatische Kanaländerungen.
- API-Authentifizierung, lokale Bindung, Docker Compose, eingefrorene Migration, Dependency-Constraints und Tests.

## Wichtige Grenzen

**Organisch:** Data-API-Zähler enthalten Gesamtviews. Der Traffic-Bericht weist nicht als Werbung klassifizierte Views separat aus. Erkannter Werbetraffic setzt neue Gesamtzähler-Prognosen und den Momentum Score aus. Fehlende Traffic-Daten sind kein Nachweis rein organischer Views. Es werden keine Werbeaktionen durchgeführt.

**Wahrscheinlichkeiten:** 100.000/1.000.000 beziehen sich jeweils auf das Erreichen des Gesamtzählers bis zum angegebenen Prognosezeitpunkt. Ohne mindestens 30 unabhängige, passende Modellfehler werden keine Zahlen ausgegeben. Auch danach muss die Ereignisschwelle ausreichend innerhalb des empirischen Bereichs liegen; extreme seltene Ereignisse bleiben häufig „noch unkalibriert“. Das ist keine Garantie statistischer Zuverlässigkeit.

**Kausalität:** Vergleichsvideos sind Quasi-Experimente, keine randomisierten Zuschauergruppen. Unterschiede können an Thema, Zeitpunkt oder Publikum liegen. „Wiederholt vielversprechend“ heißt niemals „bewiesen“.

**APIs:** Thumbnail-Impressionen und CTR kommen aus Reporting-Berichten, nicht aus Karten- oder Werbe-Impressionen. Historische Reach-Daten sind nur soweit verfügbar importierbar, wie Google sie bereitstellt. Returning Viewers und individueller langfristiger Zuschauerwert bleiben nicht verfügbar. Umsatz setzt Berechtigung und Monetarisierung voraus.

Mehr: [Architektur und Statistik](docs/ARCHITECTURE.md), [Zugangsdaten](docs/SETUP.md), [technische Prüfung und nächste Schritte](docs/TECHNICAL_REVIEW.md).

## Tests

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pip check
```

Die bereitgestellte GitHub-Actions-Datei führt zusätzlich Migrationen und Demo-Import gegen einen PostgreSQL-Service aus, wenn dieses Projekt als Repository eingerichtet und der Workflow ausgelöst wird. Sie wurde hier nicht auf GitHub ausgeführt.


## GitHub-Veröffentlichung

Das Repository ist für eine **private** GitHub-Ablage vorgesehen. Ein lokaler Commit veröffentlicht nichts; Repository-Erstellung und Push benötigen eine separate Freigabe. Vor jedem Push `git status` und den gestagten Diff prüfen. Nur die rootseitige `.env.example` darf versioniert werden; alle echten `.env`-Dateien, OAuth-Dateien, lokalen Datenbanken und Logs bleiben ausgeschlossen.

Die Zugangswerte in automatisierten Tests und der lokalen Demo sind absichtlich öffentliche Test-Fixtures und dürfen nicht für echte Daten verwendet werden.
