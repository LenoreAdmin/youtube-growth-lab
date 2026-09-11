# Technische Abschlussprüfung · 11.09.2026

## Fertig implementiert

- Modulare Python/FastAPI-Anwendung mit PostgreSQL-Datenmodell, 15 Tabellen, eingefrorener Alembic-Migration und Docker-Compose-Konfiguration.
- Offizielle YouTube-Clients für Data API, Analytics API und ergänzende Reporting-Reichweite. Desktop-OAuth, Read-only-Scopes, begrenzte HTTP-Timeouts und Pagination.
- Historischer Backfill, Import-Cursor, Wiederholung der letzten 30 Tage, idempotente Speicherung, kumulative Snapshots und isolierte Teilfehler.
- Momentum, View Velocity, Beschleunigung, Conversion, Watchtime-Effizienz und Kanalvergleich nach Format/Alter.
- Organische Traffic-Berichtswerte getrennt von Werbung und Gesamtzählern; keine neue Gesamtzähler-Prognose bei erkanntem Werbetraffic.
- Prognosen 24h/7d/30d, persistente Soll-Ist-Auswertung, adaptive Fehlerkorrektur, validierter Ridge-Challenger, empirische Intervalle und vorsichtige Schwellenwahrscheinlichkeiten.
- Experiment Memory mit allen angefragten Strategiefeldern, unveränderlicher Vorregistrierung, Ergebnissen, Abweichungen, vermuteten Ursachen, Confidence, Folge-Hypothesen und Interpretationseinträgen.
- Fehlschläge und Erfolge beeinflussen die Strategieevidenz. Wiederholungen desselben Videos/Kontrollvideos werden nicht als beliebig viele unabhängige Belege gezählt.
- Dashboard mit Video-Details, Retention, Traffic-Quellen, Prognosen, fehlenden Daten, Monetarisierung und nutzbaren Memory-Formularen.
- Single-Owner-Authentifizierung, Sicherheitsheader, ausgeschlossene Secret-Dateien, lokale Demo und Setup-Dokumentation.
- Erweiterungsverträge für Bayesian Optimization, Modelle und Experiment-Policies; Offline-Thompson-Vorschlag. Diese Erweiterungen veröffentlichen nichts.

## Tatsächlich getestet

**60 automatisierte Tests erfolgreich, 1 PostgreSQL-Integrationstest lokal übersprungen**, unter Python 3.12 in einer eigenen virtuellen Umgebung.

Abgedeckt:
- korrekte Einheiten von Velocity und Acceleration;
- fehlende Daten, Nullnenner, lange Messlücken und Zählerkorrekturen;
- gewichtete CTR und idempotenter Reporting-Import;
- paginierte API-Ergebnisse und erlaubte Download-Hosts;
- komplette Pipeline mit offiziellen Antwortformaten als Mocks;
- erneuter Import ohne doppelte Snapshots;
- Erhalt bestehender Analytics bei API-Fehlern;
- Schutz vor Mischung verschiedener Kanäle;
- Werbung als Ausschluss für organisches Momentum;
- kalter Prognosestart ohne erfundene Wahrscheinlichkeiten;
- fällige Auswertung mit begrenzter Zeitabweichung;
- adaptive Änderung späterer Prognosen durch tatsächliche Ergebnisse;
- Schutz vor Zukunftsdaten und stündlicher Scheinstichproben-Vergrößerung;
- tatsächlicher Fit und zeitliche Prüfung des Ridge-Challengers;
- konservative Wahrscheinlichkeitsausgabe außerhalb empirischer Unterstützung;
- Entwürfe vor einem Upload, unveränderliche Registrierung, gespeicherte Fehlschläge;
- kontrollierte Vergleichsauswertung und Einfluss negativer Evidenz;
- kein „Beweis“ durch Einzelerfolg oder Wiederholungen desselben Videos;
- API-Zugriffsschutz und Eingabevalidierung;
- vollständiges Upgrade → Downgrade → Upgrade der isolierten Testdatenbank;
- Übereinstimmung des migrierten Schemas mit den ORM-Modellen;
- PostgreSQL-SQL-Kompilation sämtlicher Tabellen und Indizes;
- reproduzierbarer Vorschlagsmodus des Bandit-Moduls.

Zusätzliche Vercel-Prüfungen dieser Änderung:
- Cron-Authentifizierung mit unabhängigem Secret, Ablehnung von Query-Tokens und Preview-Aufrufen;
- Lease-Ablauf, überlappende Aufrufe, stündliche Idempotenz und 336 simulierte Stunden (14 Tage; kein realer Langzeittest);
- persistente Checkpoints nach Zeitbudget-Abbruch und Fortsetzung im nächsten Lauf;
- OAuth aus Environment Variables einschließlich gemocktem Token-Refresh ohne Secret-Dateien;
- Produktionskonfiguration lehnt SQLite ab; Build verlangt explizite Deployment-Umgebung;
- additive Migration wiederholt ausgeführt, vorhandene Daten bleiben erhalten;
- Reach-Berichte werden anhand ihrer Report-ID nur einmal importiert.

Bereits bei V1 ausgeführte weitere Prüfungen:
- Python-Quellen und Migrationen kompilieren.
- JavaScript-Syntaxprüfung bestanden.
- `pip check`: keine defekten Abhängigkeitsanforderungen.
- Lokale Demo-Migration und Seed ausgeführt.
- FastAPI-Server gestartet; Startseite, Healthcheck, authentifiziertes Dashboard und Video-Detail jeweils HTTP 200.
- Dependency-Versionen in `constraints.txt` festgehalten.
- Projekt als editierbares Python-Paket erfolgreich gebaut und installiert.
- Docker-Compose-Konfiguration mit isolierter Beispielkonfiguration erfolgreich validiert.
- Quellcode-ZIP auf Lesbarkeit und Ausschluss von OAuth-/Umgebungs-Secrets geprüft.

Zwei verbleibende Warnungen stammen aus den Testclient-Abhängigkeiten: Starlette empfiehlt künftig einen anderen HTTP-Testclient und eine neue AnyIO-Importstelle. Die Anwendungstests bestehen; diese Hinweise werden nicht unterdrückt.

## Noch nicht gegen reale Systeme geprüft / bekannte Grenzen

1. **Kein echter YouTube-OAuth-Zugang:** Live-Quota, tatsächliche Kanalberechtigungen, Google-Datenunterdrückung und alle realen Berichtskombinationen müssen beim ersten Import bestätigt werden. Mocks ersetzen diesen Abnahmeschritt nicht.
2. **Kein laufender PostgreSQL-Dienst:** Docker ist vorhanden, der Daemon läuft hier jedoch nicht. PostgreSQL-DDL ist geprüft; Containerstart, echte PostgreSQL-Transaktionen und Advisory-Locks wurden lokal nicht ausgeführt. Der CI-Workflow prüft nun auch Release-Migrationen und konkurrierende Leases gegen PostgreSQL; dieser CI-Lauf ist hier noch nicht ausgeführt.
3. **Keine Browser-Automationsprüfung:** HTTP, statische Assets, JavaScript-Syntax und API-Flows sind geprüft; pixelgenaue Darstellung, Tastaturinteraktion und verschiedene Browsergrößen wurden nicht automatisiert geprüft.
4. **Keine belastbare Aussage zu deinem 20.000-Views-Video:** Ohne dessen ID und echten Daten gibt es noch keine inhaltliche Kanalanalyse.
5. **Statistische Grenzen:** Kleine Stichproben, veränderte Zuschauerinteressen und seltene Ausreißer begrenzen Prognosen. Kontrollvideos sind keine Zuschauer-Randomisierung. Confidence ist eine bedingte Modellgröße, kein Kausalitätsbeweis.
6. **Datenverfügbarkeit:** Returning Viewers und individueller langfristiger Zuschauerwert fehlen; Reach-Historie hängt von Google ab. Fehlende Revenue-Berechtigung wird isoliert behandelt.
7. **Produktionsbetrieb:** Backups, Wiederherstellung, Cron-Fehlerüberwachung, OAuth-Widerruf/Löschworkflow und gegebenenfalls Mehrbenutzer-Authentifizierung müssen für den konkreten Dauerbetrieb ergänzt werden.
8. **Erweiterungen:** XGBoost, echte Bayesian Optimization und automatische Bandit-Zuweisung sind nicht aktive Produktivfunktionen. Es gibt dafür optionale Abhängigkeiten, Datenmodell und Interfaces.
9. **Skalierung:** Für einen kleinen Kanal geeignet. Reach-Downloads haben persistente Importbelege. Für große Kataloge bleiben Bulk-Import, zusätzliche Metadaten-Checkpoints und separate Trainingsjobs sinnvoll.
10. **Experimentziele:** V1 wertet automatisch zusätzliche Views gegen eine vorab registrierte Erwartung aus. Kontrollierte Experimente mit anderen primären Kennzahlen, Attribution und randomisierte Zuweisung benötigen ein weiteres Modul.

## Extern noch erforderlich

- Google-Cloud-Projekt mit aktivierten YouTube-APIs.
- Einmalige interaktive OAuth-Autorisierung; Client-ID, Client-Secret und Refresh-Token danach ausschließlich als Vercel Environment Variables. Consent-Status nicht Testing (7-Tage-Ablauf beachten).
- Kanal-ID sowie Video-ID des vorhandenen Videos mit ungefähr 20.000 Views.
- Persistentes Cloud-PostgreSQL mit DATABASE_URL und direkter MIGRATION_DATABASE_URL.
- Unabhängige zufällige APP_TOKEN und CRON_SECRET; Vercel Pro/Enterprise für stündliche Cron-Ausführung. Kein Tarif wurde gebucht.
- Optional später: Monetarisierungsberechtigung plus Monetary-Readonly-Scope.

## Nächste Schritte mit dem größten Nutzen

1. Echten Kanal verbinden und das 20.000-Views-Video als Fokus setzen; historische Tagesdaten, Quellen und Retention importieren.
2. 48–72 Stunden aktuelle Snapshots sammeln und Datenvollständigkeit prüfen. Vercel Cron übernimmt nach dem Production-Deployment die stündliche Erfassung.
3. Für das Fokusvideo frühe Retention-Verluste, dominierende Traffic-Quellen, tatsächliche Conversion und gegebenenfalls Thumbnail-Reichweite untersuchen.
4. Für die nächsten Videos jeweils eine klare Hypothese vorab registrieren. Möglichst eine Strategiedimension ändern, passende Vergleiche festlegen und alle Fehlschläge mit auswerten.
5. Prognosefehler und Unsicherheitsabdeckung pro Horizont verfolgen. Komplexere Modelle nur bei nachweislichem Vorteil gegenüber den einfachen Baselines aktivieren.
6. Bei verfügbarer Monetarisierung RPM und Umsatz separat hinzufügen; langfristigen Zuschauerwert erst mit einer belastbaren Definition und geeigneten Daten modellieren.

Es wurden keine kostenpflichtigen externen Aktionen ausgeführt und keine Inhalte oder Interaktionen auf deinem YouTube-Kanal verändert.

## Vercel-Umstellung

FastAPI-Einstieg und Dashboard bleiben erhalten. vercel.json ersetzt den dauerhaften Worker im Hosting durch GET /api/cron/sync. Der Worker bleibt nur eine ausdrücklich gewählte lokale Alternative. PostgreSQL-Leases, Checkpoints und Report-Importbelege überleben Function-Neustarts. Migrationen laufen ausschließlich im Production-Build mit Transaktionssperre und anschließendem Schemaabgleich, niemals beim HTTP-Aufruf.

Es wurde kein Vercel-Deployment und keine produktive Datenbankmigration ausgeführt: Cloud-Datenbank und Google-Umgebungsvariablen sind hier nicht eingerichtet. Der lokale PostgreSQL-Integrationstest wird deshalb übersprungen; SQLite wird nur für lokale Tests verwendet. Vercel-Bundlegröße und reales Function-Laufzeitverhalten sind beim ersten Deployment zu prüfen. Ein stündlicher Zeitplan garantiert weder minutengenaue Ausführung noch unterbrechungsfreien Betrieb; Fehleralarme und Prüfung des letzten erfolgreichen Syncs sind erforderlich. Einrichtung: [VERCEL.md](VERCEL.md).
