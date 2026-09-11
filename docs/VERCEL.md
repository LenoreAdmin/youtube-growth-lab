# Vercel-Betrieb: stündlicher Sync ohne laufenden PC

## Voraussetzungen

- Vercel **Pro oder Enterprise**: Hobby unterstützt keine stündlichen Cron-Jobs. Es wird kein Tarif automatisch gebucht.
- Persistente PostgreSQL-Datenbank, z. B. ein bereits vorhandener Neon-/Supabase-/anderer PostgreSQL-Dienst. Keine SQLite-Datei in Production oder Preview.
- Google Cloud: YouTube Data API v3, Analytics API und optional Reporting API aktiviert.
- Langlebiger OAuth-Refresh-Token des Kanalinhabers. Ein Google-Projekt mit OAuth-Zustimmungsbildschirm im **Testing**-Status kann Refresh-Tokens nach sieben Tagen verlieren. Für 14 Tage unbeaufsichtigten Betrieb den geeigneten Production-Status und gegebenenfalls erforderliche Google-Verifikation herstellen und danach erneut autorisieren. Widerruf bleibt jederzeit möglich.
- Zielrepository gemäß bisheriger Vorgabe **privat** schalten. Dies ist eine GitHub-Einstellung; `vercel.json` kann sie nicht erzwingen.

## Einmalige Einrichtung in Vercel

1. Bestehendes GitHub-Repository importieren. Wenn das Repository diesen Projektinhalt direkt enthält, ist die Root Directory `.` und Framework **FastAPI**. Nicht den lokalen übergeordneten Codex-Ordner auswählen.
2. Production Branch auf `main` setzen. Fluid Compute aktiv lassen. Build Command aus `vercel.json` übernehmen.
3. „Automatically expose System Environment Variables“ einschalten, damit insbesondere `VERCEL_ENV` vorhanden ist.
4. Folgende Environment Variables im **Production**-Scope setzen. Geheimwerte über Vercels geschützte Eingabefelder setzen, nicht über Git, Commitnachrichten, URLs oder Chat:

| Variable | Wert / Zweck |
|---|---|
| APP_ENV | `production` |
| DATABASE_URL | Persistentes PostgreSQL; `postgresql://...` wird zu psycopg normalisiert. Pooled URL möglich |
| MIGRATION_DATABASE_URL | Direkter PostgreSQL-Endpunkt derselben Datenbank, DDL-Berechtigung; kein Transaction-Pooler |
| APP_TOKEN | Zufälliges Dashboard-Secret mit mindestens 32 Zeichen |
| CRON_SECRET | Anderes zufälliges Secret mit mindestens 32 Zeichen |
| GOOGLE_CLIENT_ID | OAuth-Client-ID |
| GOOGLE_CLIENT_SECRET | OAuth-Client-Secret |
| GOOGLE_REFRESH_TOKEN | Zum Client gehörender autorisierter Refresh-Token |
| CHANNEL_ID | Erwartete eigene Kanal-ID |
| FOCUS_VIDEO_ID | ID des Videos mit etwa 20.000 Views, falls bekannt |
| ENABLE_REVENUE | `false`, später bei Berechtigung `true` |
| ENABLE_REACH | `true` |
| ANALYTICS_LAG_DAYS | `3` |
| SYNC_INTERVAL_SECONDS | `3600`; dient auch der Anzeige veralteter Snapshots |
| SYNC_BUDGET_SECONDS | `210`, erlaubt 30–210 |

TLS wird für gehostete PostgreSQL-Verbindungen standardmäßig mit `sslmode=require` eingeschaltet, sofern die URL keine explizite SSL-Konfiguration enthält. Für strengere Zertifikatsprüfung eine providerkonforme `verify-full`-Konfiguration verwenden.

Der Code erstellt keine Secret-Dateien in Vercel. Er konstruiert Google Credentials aus den drei Variablen und aktualisiert Access-Tokens im Arbeitsspeicher. Der Refresh-Token muss auch bei einem Cold Start erneut verfügbar sein; ein kurzlebiger Access-Token allein genügt nicht.

Die einmalige interaktive OAuth-Autorisierung darf weiterhin lokal per `python -m app.cli oauth` erfolgen. Die dabei erzeugten Dateien sind ignoriert und dürfen nicht committed werden. Anschließend die Werte über Vercels UI übertragen. `GOOGLE_CLIENT_FILE` und `GOOGLE_TOKEN_FILE` werden in gehosteten Umgebungen niemals als Fallback gelesen.

5. Deployment auslösen. Es startet keinen Dauer-Worker. Keine zweite Cron-Definition oder externen Ping-Service parallel aktivieren.
6. Cron-Lauf in Vercel kontrollieren und im Dashboard prüfen, dass tatsächliche Snapshots und Importstatus erscheinen. Erst nach erfolgreichem Live-Test ist der unbeaufsichtigte Betrieb abgenommen.

## Sichere Migrationen

`buildCommand: python -m app.release build` läuft nach der Python-Abhängigkeitsinstallation und **vor** dem Umschalten des Deployments.

- Nur `VERCEL_ENV=production` führt Migrationen aus.
- Google-Konfiguration, Kanal-ID und Migration-URL müssen vollständig vorliegen.
- `MIGRATION_DATABASE_URL` ist für den Produktionsbuild zwingend.
- Auf derselben direkten PostgreSQL-Verbindung: Transaktion, transaktionsgebundener Advisory-Lock, 10s Lock-Timeout, 60s Statement-Timeout und `alembic upgrade head`.
- Fehler rollen die Transaktion zurück und beenden den Build mit Exitcode 1. Keine Secrets in Fehlerausgaben.
- Danach wird über `DATABASE_URL` geprüft, ob die Laufzeitdatenbank exakt dieselbe Schema-Version erreicht hat. Versehentlich verschiedene Datenbankziele führen zum Buildfehler.
- Kein `create_all`, kein automatischer Downgrade und keine Migration in einem HTTP-Request oder Cold Start.
- Die neue Migration `0002` ergänzt nur `job_leases` und `imported_reports`; vorhandene Kanal-, Snapshot- und Experimentdaten bleiben bestehen.
- Preview-Builds führen **keine** Migration aus. Eine eigene Preview-Datenbank separat per `python -m app.release migrate` vorbereiten. Production-Secrets niemals in den Preview-Scope kopieren. Preview-Cron wird auch bei gültiger Authentifizierung mit 403 abgewiesen.
- Vor dem ersten Produktionsupgrade Datenbankbackup bzw. Provider-Wiederherstellungspunkt prüfen. Zukünftige Änderungen ebenfalls nach dem Expand/Contract-Prinzip vornehmen: ein fehlgeschlagenes Deployment darf die alte laufende Version nicht inkompatibel machen.

Lokale Tests prüfen Migrationen und Datenerhalt mit SQLite. Der CI-Workflow hat zusätzlich einen echten PostgreSQL-Service und prüft Migrationen plus konkurrierende Leases. Ein lokal nicht verfügbarer PostgreSQL-Dienst wird als übersprungener Integrationstest gemeldet.

## Cron-Vertrag

```json
{"path": "/api/cron/sync", "schedule": "0 * * * *"}
```

Vercel ruft die Route stündlich per GET auf. Der Scheduler setzt automatisch `Authorization: Bearer <CRON_SECRET>`. Queryparameter, User-Agent oder Dashboard-Token reichen nicht aus. Vergleiche erfolgen zeitkonstant. Antworten sind nicht cachebar.

Status:
- `ok`: abgeschlossen, HTTP 200.
- `already_running` / `already_completed`: paralleler bzw. doppelter Aufruf ohne zusätzliche Arbeit, HTTP 200.
- `deferred`: Zeitbudget erreicht; bereits bestätigte Daten bleiben gespeichert, Fortsetzung beim nächsten Stundentermin, HTTP 200.
- `partial` / `failed`: Fehler, HTTP 503; Details sind auf harmlose Fehlerklassen begrenzt.

Die Function darf maximal 300 Sekunden laufen. Das Arbeitsbudget liegt bei 210 Sekunden; vor einer weiteren Netzwerkoperation bleiben mindestens 25 Sekunden Reserve. Google-Anfragen haben 15 Sekunden Socket-Timeout und keine automatischen langen Retries. Fehlgeschlagene Fenster werden beim nächsten Cron erneut abgefragt.

## Zuverlässigkeit und Grenzen

- Eine 360-Sekunden-Datenbanklease verhindert parallele Syncs auch über mehrere Vercel-Instanzen und Transaction-Pooler hinweg. Ein abgebrochener Prozess blockiert den nächsten Stundenlauf nicht dauerhaft. Nur der jeweilige Lease-Inhaber darf freigeben.
- Ein persistenter UTC-Stundenbezeichner macht erfolgreich abgeschlossene Stundenaufrufe idempotent.
- Bestehende historische Cursor bleiben nutzbar. Videos werden nach letztem Bearbeitungsversuch rotiert, damit ein großer Backfill andere Videos nicht dauerhaft verdrängt.
- Erfolgreiche Analytics-Fenster und Reach-Downloads werden einzeln bestätigt. Neue Report-IDs werden importiert; bereits importierte IDs nicht erneut heruntergeladen. Google-Korrekturen mit neuen Report-IDs bleiben möglich.
- Snapshots werden vor dem historischen Backfill erfasst. Umfangreiche Backfills können mehrere Stundenläufe benötigen.
- Datenbankverbindungen verwenden NullPool und keine vorbereiteten Session-Statements. Der Sync benötigt keinen dauerhaft gebundenen Datenbankprozess.
- Vercel garantiert weder exakte Ausführungszeit noch automatische Wiederholung fehlgeschlagener Cron-Aufrufe. Ausfälle erzeugen echte Messlücken; keine künstlichen historischen Snapshots.
- Der 336-Aufrufe-Test beweist die Zustandslogik, nicht eine 14-tägige Verfügbarkeit von Google, Vercel oder PostgreSQL.
- Dauerhaft `partial`, `failed` oder `deferred` in Dashboard/Logs untersuchen. Vercel-Fehlerüberwachung/Benachrichtigungen aktivieren und Datenbankquota sowie Backup überwachen.
- Sehr große Kanäle können mehr Paging-/Jobgranularität benötigen. Der hier gewählte Funktionsansatz ist für den aktuellen kleinen Kanal ausgelegt; kein unbegrenzter Batch-Worker.

## Referenzen

- [FastAPI auf Vercel](https://vercel.com/docs/frameworks/backend/fastapi)
- [Python Runtime](https://vercel.com/docs/functions/runtimes/python)
- [Cron-Tarifgrenzen](https://vercel.com/docs/cron-jobs/usage-and-pricing)
- [Cron-Authentifizierung und Fehlerverhalten](https://vercel.com/docs/cron-jobs/manage-cron-jobs)
- [Google-Refresh-Token-Laufzeiten](https://developers.google.com/identity/protocols/oauth2#expiration)
