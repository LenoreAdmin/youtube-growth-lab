# YouTube Growth Lab Â· V1

Ein lokal betreibbares System fÃ¼r echte YouTube-Daten, Wachstumssignale, Prognosen und ein lernendes Experiment Memory. Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy/Alembic und scikit-learn. Die OberflÃ¤che ist deutschsprachig und wird direkt von FastAPI ausgeliefert.

**Status:** ausfÃ¼hrbare V1 mit getesteter Import- und Lernlogik. Ohne deinen OAuth-Zugang wurden keine echten Kanaldaten abgerufen. Die Demo ist vollstÃ¤ndig synthetisch. Keine Videos, Titel, Thumbnails, Kommentare oder sonstigen Kanalinhalte werden verÃ¶ffentlicht oder geÃ¤ndert.

## Empfohlen: Vercel mit stündlichem Cron

**Der PC wird für den laufenden Betrieb nicht benötigt.** Vercel betreibt die FastAPI-App und ruft `/api/cron/sync` stündlich auf. Dauerhafte Daten liegen in externem PostgreSQL, Google-OAuth-Zugangsdaten in Vercel Environment Variables. SQLite ist auf Vercel gesperrt.

Die vollständige Einrichtung steht in **[docs/VERCEL.md](docs/VERCEL.md)**. Dafür werden Vercel Pro/Enterprise, PostgreSQL und ein ausreichend langlebiger Google-Refresh-Token benötigt. Es wird kein kostenpflichtiger Dienst automatisch eingerichtet.

`vercel.json` enthält Cron und Laufzeitgrenze. Produktionsbuilds führen die additiven Migrationen sicher und gesperrt über einen direkten PostgreSQL-Zugang aus; Preview-Builds verändern kein Schema. Fortschritt, Doppelausführungsschutz und Leases liegen in der Datenbank. Eine harte Funktionsunterbrechung hängt den Sync nicht dauerhaft auf.

## Alternative: lokaler Docker-Betrieb

1. Python 3.12 und Docker Desktop mit laufendem Docker-Dienst bereitstellen.
2. Im Projektverzeichnis:
   ```powershell
   python -m venv .venv
   .venv/Scripts/python.exe -m pip install --no-cache-dir -c constraints.txt ".[dev]"
   Copy-Item .env.example .env
   ```
3. **Alle** `REPLACE_WITH_...`-Werte in `.env` ersetzen, einschlieÃŸlich Pfaden, booleschen Schaltern und Zahlen. Empfohlene Werte stehen in [SETUP.md](docs/SETUP.md). Die Vorlage enthÃ¤lt bewusst keine betriebsfertigen Werte. AnschlieÃŸend ein eigenes `POSTGRES_PASSWORD` und die identische URL-kodierte Passwortkomponente in `DATABASE_URL` setzen. Ein zufÃ¤lliges alphanumerisches Passwort vermeidet URL-Sonderzeichen. `APP_TOKEN` unabhÃ¤ngig davon erzeugen:
   ```powershell
   .venv/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Diesen Wert nur in der lokalen `.env` ablegen. `CHANNEL_ID` auf den gewÃ¼nschten Kanal und `FOCUS_VIDEO_ID` auf das vorhandene Video mit ca. 20.000 Views setzen.
4. Google-Zugang gemÃ¤ÃŸ [SETUP.md](docs/SETUP.md) einrichten; OAuth-Clientdatei nach `secrets/client_secret.json` legen.
   ```powershell
   .venv/Scripts/python.exe -m app.cli oauth
   ```
5. Nur bei bewusst lokalem Betrieb PostgreSQL, Migration, API und optionalen Worker starten:
   ```powershell
   docker compose --profile local-worker up --build -d
   docker compose logs --tail 80 worker
   ```
6. [Dashboard Ã¶ffnen](http://127.0.0.1:8000), mit deinem `APP_TOKEN` anmelden. Der Worker synchronisiert standardmÃ¤ÃŸig stÃ¼ndlich. Erst nach genÃ¼gend zeitlich getrennten Snapshots entstehen aktuelle Wachstumsmetriken.

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

SQLite dient ausschlieÃŸlich Tests und der lokalen Demo; der dauerhafte Betrieb verwendet PostgreSQL. Der Demo-Importer verweigert eine bereits befÃ¼llte Datenbank; die echte Pipeline verweigert einen Kanalwechsel in derselben Datenbank. Demo-Variablen in einer neuen Shell nicht Ã¼bernehmen.

## Enthalten

- Offizielle Data-, Analytics- und ergÃ¤nzende Reporting-API-Adapter mit OAuth, Pagination, Zeitlimits und Retries.
- Upload-Metadaten, kumulative Snapshots, historische Tagesdaten, Audience Retention, Traffic-Quellen, Reichweite/CTR und optional Umsatz.
- Inkrementelle Importe mit 30 Tagen Ãœberlappung fÃ¼r Datenkorrekturen, eindeutigen SchlÃ¼sseln und Importstatus.
- View Velocity, Beschleunigung, Conversion, Watchtime-Effizienz, vergleichbare Kanal-Kohorten und erklÃ¤rter Momentum Score.
- Gespeicherte Prognosen fÃ¼r 24 Stunden, 7 Tage und 30 Tage einschlieÃŸlich spÃ¤terem Soll-Ist-Vergleich.
- Adaptiver Basisansatz, zeitlich validierter Ridge-Challenger und empirische Unsicherheitsbereiche.
- Experiment Memory mit unverÃ¤nderlicher Vorregistrierung, Kontrollvideo, Ergebnissen, Fehlern, Confidence, Interpretationshistorie und nÃ¤chster Hypothese.
- Separate Monetarisierung; vorgeschlagene Bandit-Experimente und Schnittstellen fÃ¼r XGBoost/Bayesian Optimization, ohne automatische KanalÃ¤nderungen.
- API-Authentifizierung, lokale Bindung, Docker Compose, eingefrorene Migration, Dependency-Constraints und Tests.

## Wichtige Grenzen

**Organisch:** Data-API-ZÃ¤hler enthalten Gesamtviews. Der Traffic-Bericht weist nicht als Werbung klassifizierte Views separat aus. Erkannter Werbetraffic setzt neue GesamtzÃ¤hler-Prognosen und den Momentum Score aus. Fehlende Traffic-Daten sind kein Nachweis rein organischer Views. Es werden keine Werbeaktionen durchgefÃ¼hrt.

**Wahrscheinlichkeiten:** 100.000/1.000.000 beziehen sich jeweils auf das Erreichen des GesamtzÃ¤hlers bis zum angegebenen Prognosezeitpunkt. Ohne mindestens 30 unabhÃ¤ngige, passende Modellfehler werden keine Zahlen ausgegeben. Auch danach muss die Ereignisschwelle ausreichend innerhalb des empirischen Bereichs liegen; extreme seltene Ereignisse bleiben hÃ¤ufig â€žnoch unkalibriertâ€œ. Das ist keine Garantie statistischer ZuverlÃ¤ssigkeit.

**KausalitÃ¤t:** Vergleichsvideos sind Quasi-Experimente, keine randomisierten Zuschauergruppen. Unterschiede kÃ¶nnen an Thema, Zeitpunkt oder Publikum liegen. â€žWiederholt vielversprechendâ€œ heiÃŸt niemals â€žbewiesenâ€œ.

**APIs:** Thumbnail-Impressionen und CTR kommen aus Reporting-Berichten, nicht aus Karten- oder Werbe-Impressionen. Historische Reach-Daten sind nur soweit verfÃ¼gbar importierbar, wie Google sie bereitstellt. Returning Viewers und individueller langfristiger Zuschauerwert bleiben nicht verfÃ¼gbar. Umsatz setzt Berechtigung und Monetarisierung voraus.

Mehr: [Architektur und Statistik](docs/ARCHITECTURE.md), [Zugangsdaten](docs/SETUP.md), [technische PrÃ¼fung und nÃ¤chste Schritte](docs/TECHNICAL_REVIEW.md).

## Tests

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pip check
```

Die bereitgestellte GitHub-Actions-Datei fÃ¼hrt zusÃ¤tzlich Migrationen und Demo-Import gegen einen PostgreSQL-Service aus, wenn dieses Projekt als Repository eingerichtet und der Workflow ausgelÃ¶st wird. Sie wurde hier nicht auf GitHub ausgefÃ¼hrt.


## GitHub-VerÃ¶ffentlichung

Das Repository ist fÃ¼r eine **private** GitHub-Ablage vorgesehen. Ein lokaler Commit verÃ¶ffentlicht nichts; Repository-Erstellung und Push benÃ¶tigen eine separate Freigabe. Vor jedem Push `git status` und den gestagten Diff prÃ¼fen. Nur die rootseitige `.env.example` darf versioniert werden; alle echten `.env`-Dateien, OAuth-Dateien, lokalen Datenbanken und Logs bleiben ausgeschlossen.

Die Zugangswerte in automatisierten Tests und der lokalen Demo sind absichtlich Ã¶ffentliche Test-Fixtures und dÃ¼rfen nicht fÃ¼r echte Daten verwendet werden.

V2-Erweiterung, Testergebnis und sichere Production-Abnahme: [docs/V2.md](docs/V2.md).
