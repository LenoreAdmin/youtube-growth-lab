# Historischer Backfill

Der Backfill lädt YouTube-Historie nach, die Google über die offiziellen APIs noch liefert, damit das System nicht ausschließlich auf neu entstehende Daten warten muss. Er ersetzt keine Beobachtung: **Snapshots (kumulative Zähler) bleiben ausschließlich echte Beobachtungszeitpunkte.** Es werden keine rückdatierten Snapshots erzeugt, keine Zählerstände rekonstruiert und keine Prognosen nachträglich erfunden.

## Was tatsächlich rückwirkend verfügbar ist

| Daten | Quelle | Rückwirkend | Ablage |
|---|---|---|---|
| Tägliche Views, Watchtime, Ø Wiedergabedauer, Ø Prozent, Abo-Gewinn/-Verlust, Likes, Kommentare | Analytics API `day` | bis zum Upload-Datum des Videos (Pacific Time), soweit Google Zeilen liefert; Ende = heute − `ANALYTICS_LAG_DAYS` | `video_daily` – **nur fehlende Tage vor der frühesten vorhandenen Zeile**, nie Überschreiben |
| Traffic-Quellen je Tag (inkl. Views, Watchtime) | Analytics API `day,insightTrafficSourceType` | bis zum Upload-Datum | `video_traffic_daily`, Spalte `paid` = `ADVERTISING` |
| Audience Retention | Analytics API `elapsedVideoTimeRatio` | bis zum Upload-Datum, **nur je Zeitraum** (vollständige Kalendermonate) | `backfill_reports` (`kind="retention"`) |
| Thumbnail-Impressionen / CTR | Reporting API | **nur 30 Tage vor Erstellung des Reporting-Jobs**; Berichte 60 Tage abrufbar | `reach_daily`, ausschließlich über den stündlichen Sync (der Backfill zeigt nur die Abdeckung) |
| Kumulative Zähler (Snapshots) | Data API | nicht rückwirkend | — |
| Returning Viewers, Zuschauer-Lifetime-Value | — | nicht über die APIs | — |

Der stündliche Sync importiert Tageswerte bereits ab Upload-Datum; der Backfill schließt nur Lücken davor und fügt die bisher fehlenden tagesgenauen Traffic-Quellen und die Retention-Historie hinzu.

## Ablauf

`POST /api/backfill` (Dashboard-Token `APP_TOKEN`, in Preview-Deployments gesperrt) startet einen Lauf mit dem gleichen Zeitbudget wie der Sync (`SYNC_BUDGET_SECONDS`). Der Lauf nutzt eine eigene Datenbank-Lease `youtube-backfill`; der Vercel-Cron und `/api/sync` werden nicht blockiert. `vercel.json` und der Cron bleiben unverändert; der Backfill läuft nur manuell.

Reihenfolge je Lauf: je Video `daily` → `traffic` → `retention`. Fokusvideo zuerst, danach Videos mit den wenigsten Fehlversuchen, neueste Uploads zuerst. Reach-Berichte importiert nur der stündliche Sync; damit hat jede Tabelle genau einen Schreiber außer `video_daily`.

## Konfliktfreiheit gegenüber dem Cron-Sync

- **Datumsdisjunkte Fenster:** Der Sync löscht und schreibt je Video nur `[Cursor − 30 Tage, Ende]`. Die `daily`-Stufe des Backfills schreibt ausschließlich Tage **mindestens 45 Tage unterhalb des Sync-Cursors** und unterhalb der frühesten vorhandenen Zeile. Videos, die der Sync noch nie verarbeitet hat (kein Cursor, keine Zeilen), bleiben `pending` („Wartet auf ersten Sync des Videos“), weil der Sync dort selbst die gesamte Historie lädt.
- **Atomare Upserts auf Datenbankebene:** `video_daily` wird mit `INSERT … ON CONFLICT (video_id, day) DO NOTHING` geschrieben (PostgreSQL und SQLite nativ) – eine parallel eingefügte Zeile bleibt unverändert, es entsteht kein `IntegrityError`. `video_traffic_daily` und `backfill_reports` verwenden `ON CONFLICT … DO UPDATE` mit identischen Werten; ein Wiederholungslauf ist damit auch ohne Cursor duplikatfrei.
- Eigene Lease `youtube-backfill`; die Sync-Lease und der Cron bleiben unverändert.

Fortschritt liegt in `backfill_progress` (Video × Art: `first`, `target`, `through`, `status`, `attempts`, `note`). Jeder erfolgreich gespeicherte Abschnitt (179 Tage Tageswerte, 90 Tage Traffic, ein Retention-Monat) wird sofort bestätigt. Ein unterbrochener Lauf setzt beim nächsten Aufruf bei `through + 1` fort. Wiederholte Läufe fragen abgeschlossene Bereiche nicht erneut ab und erzeugen keine Duplikate (Primärschlüssel je Tag/Quelle bzw. Fenster).

Status eines Laufs (`backfill_runs`):

- `ok`: alle Stufen aktuell.
- `deferred`: Zeitbudget erreicht; das Dashboard startet automatisch weitere Runden (maximal 25 pro Klick).
- `throttled`: Quota, 429 oder 5xx von Google; Fortschritt gespeichert, kein Fehlversuch gezählt, später erneut starten.
- `partial`: einzelne Video/Stufen-Fehler (z. B. Timeout). Betroffene Stufen zählen Versuche; nach 5 Fehlversuchen oder bei 400/403 werden sie als `error` übersprungen, bis ein Lauf mit `{"retry_failed": true}` sie zurücksetzt.
- `failed`: Laufebene (z. B. OAuth-Konfiguration), HTTP 503.

`GET /api/backfill` liefert Zusammenfassung, tatsächliche Abdeckung (erster/letzter Tag je Datenart, Videotage mit Werbetraffic) und den Fortschritt je Video. `/api/dashboard` enthält dieselbe Zusammenfassung; `/api/videos/{id}` liefert Traffic-Quellen-Summen, Retention-Monate und den Backfill-Stand des Videos.

## Wirkung auf V2/V3

- Growth-Fenster, Forecasts, Experiment Memory und Prediction Audits bleiben unverändert; sie arbeiten weiterhin nur mit echten Snapshots.
- `organic.eligibility` akzeptiert zusätzlich tagesgenaue Abdeckung aus `video_traffic_daily`: Ein Tag zählt nur, wenn der Traffic-Cursor ihn erreicht hat, die Quellsummen den Tageswerten in `video_daily` entsprechen und alle Zeilen vor dem Prüfzeitpunkt (`fetched_at`) vorlagen. **Jede `ADVERTISING`-Zeile mit Views im Prüfzeitraum führt zu `paid_excluded`.** Damit schließt die Historie die Lücke der ersten Tage nach dem ersten Sync, die die gleitenden 28-Tage-Berichte nie abdecken; bezahlter Traffic bleibt vom organischen Training ausgeschlossen.
- Aus Tageswerten werden keine Velocity- oder Acceleration-Werte abgeleitet; Prognosen benötigen weiterhin reale, zeitlich getrennte Snapshots.

## Migration

`0005_historical_backfill.py` ist additiv: `backfill_runs`, `backfill_progress`, `video_traffic_daily`, `backfill_reports`. Bestehende Tabellen und Daten bleiben unberührt. Der Produktionsbuild führt sie über `MIGRATION_DATABASE_URL` wie bisher aus. Keine neuen Environment-Variablen, keine neuen Secrets, keine Cron-Änderung.

## Grenzen

- Google liefert keine Impressionen/CTR vor den 30 Tagen vor Job-Erstellung; ältere Reichweite bleibt unbekannt.
- Retention gibt es nur aggregiert je Zeitraum; Monatsfenster mit null Views werden als leere Berichte gespeichert.
- Tageswerte für Tage ohne Aktivität können in der Analytics-Antwort fehlen; das ist keine Datenlücke des Systems.
- Der Backfill ist auf einen kleinen Kanal ausgelegt (mehrere Runden zu je ≤ 210 s). Sehr große Kataloge benötigen mehr Runden.
- Tageswerte innerhalb der letzten 45 Tage vor dem Sync-Cursor werden nie vom Backfill geschrieben; sie sind Domäne des Sync-Fensters.
