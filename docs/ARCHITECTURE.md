# Architektur und Methoden

## Module

```text
app/
  config.py        Umgebungsvariablen
  models.py        relationales Datenmodell
  db.py            Engine und Sessions
  youtube.py       offizielle APIs und Desktop-OAuth
  pipeline.py      historische Imports, Snapshots, Feedback-Zyklus
  metrics.py       Velocity, Acceleration, Momentum, Conversion
  prediction.py    Baseline, Rückkopplung, Ridge und Kalibrierung
  memory.py        Vorregistrierung, Auswertung, Strategieevidenz
  experiments.py   Policy-Verträge und Offline-Thompson-Vorschlag
  main.py          FastAPI, Auth und Dashboard-Endpunkte
  cli.py           oauth / sync / worker / demo
  demo.py          ausschließlich synthetische Beispieldaten
  static/          HTML, CSS, JavaScript
migrations/        eingefrorene Alembic-Migration
tests/             Rechen-, Import-, API-, Memory- und Migrationstests
```

Keine zusätzliche Node-Laufzeit im Betrieb. Diese Entscheidung hält eine interne Single-Owner-V1 einfach; die REST-API erlaubt später ein separates Frontend.

## Datenbank

| Tabelle | Zweck / Schlüssel |
|---|---|
| channels | Autorisierter Kanal und aktueller Abostand |
| videos | YouTube-ID, Upload-Zeitpunkt, Länge, Aktivstatus |
| snapshots | Kumulative Zähler; UNIQUE(video_id, observed_at), UTC |
| video_daily | Historische Analytics-Tageswerte; PK(video_id, day) |
| reports | Typ, Zeitraum, Zeilen und Abrufzeit; Retention, Traffic, Revenue |
| reach_daily | Offizielle Thumbnail-Impressionen und CTR je Video/Tag |
| ingest_cursors | Letzter erfolgreich abgefragter Zeitraum je Video/Art |
| sync_runs | Laufstatus, Zeitstempel, isolierte Fehler |
| forecasts | Unveränderliche Vorhersage, Features, Modell, tatsächliches Ergebnis |
| model_runs | Validierung, Modellparameter, Trainings-IDs, Koeffizienten/Skalierung |
| decisions | Vollständiges Experiment Memory, vorab definierte Strategie |
| memory_reviews | Nachträgliche Interpretationen append-only |
| experiments | Erweiterung für mehrarmige Experimentpläne |

Fremdschlüssel und eindeutige Schlüssel verhindern verwaiste bzw. doppelte Daten. PostgreSQL ist das Produktionsziel; SQLite dient reproduzierbaren Tests. Schemaänderungen erhalten künftig neue Migrationen; 0001 ist unabhängig von späteren Modelldefinitionen eingefroren.

Snapshots sind aktuelle Data-API-Messungen. Historische Snapshots werden nicht aus Tageswerten erfunden. Analytics-Tage folgen der YouTube-Berichtszeitzone America/Los_Angeles. Snapshots und Prognosezeitpunkte sind UTC. Der aktuelle Tagesbereich wird mit Sicherheitsabstand abgefragt; die letzten 30 Tage werden erneut geladen. Leere API-Berichte bleiben fehlende Beobachtungen und werden nicht als gemessene Nullen ausgegeben.

Historische Tagesberichte werden in 180-Tage-Fenstern paginiert. Erfolgreiche Fenster werden transaktional ersetzt. Fehler belassen bisherige Daten. Upload-Playlist-Pagination vermeidet teure Suchanfragen. Report-Reimporte ersetzen Werte anstatt sie aufzusummieren. CTR wird bei mehreren Segmenten impressionsgewichtet.

## Metriken

- View Velocity: Differenz des Gesamtzählers / tatsächlich vergangene Stunden.
- Growth Acceleration: Differenz zweier View-Velocities / Abstand der Fenstermittelpunkte; Einheit Views/h².
- Verwendet werden möglichst zwei 24h-Fenster; Intervalle unter 1h oder über 36h werden verworfen. Korrekturen nach unten in irgendeinem Zwischenintervall invalidieren die Berechnung.
- Subscriber Conversion: gewonnene Abonnenten / Analytics-Views im selben Zeitfenster.
- Watchtime Efficiency: Watchtime in Sekunden / Views / Videolänge. Das ist die hier definierte normalisierte Watchtime, nicht automatisch dieselbe Größe wie YouTubes durchschnittlicher Prozentwert. Wiederholungen können Werte über 1 erzeugen. Der offizielle Durchschnitt bleibt zusätzlich in den Tagesdaten erhalten.
- Relative Performance: aktuelle Velocity / Durchschnitt anderer Videos desselben bekannten Content-Typs und derselben groben Alterskohorte (0–29, 30–59, 60–89, 90+ Tage).
- Momentum: 50 + 50 × gewichtete Kombination aus tanh(Velocity-Trend) und tanh(log2(relative Performance)); Gewichte 65/35. Ohne geeignete Vergleichsvideos wird das verfügbare Gewicht renormalisiert. Ohne verlässliche Snapshots kein Score.
- Die Heuristik bewertet Wachstum und wird noch nicht als wissenschaftlich validierter Erfolgsindikator bezeichnet.
- Umsätze separat; RPM = Umsatz / Views desselben Revenue-Berichts × 1000, USD. Unbekannt bleibt NULL, nicht 0.

Beobachtete Traffic-Anteilsänderungen ab 10 Prozentpunkten werden als mögliche Faktoren genannt. CTR und Retention sind sichtbar, jedoch ohne ausreichendes Experiment kein Kausalnachweis. Bei Werbe-Traffic werden Gesamtzähler-Prognosen und Score ausgesetzt. Nicht als Werbung klassifizierte Views werden separat gezeigt; ein leerer Bericht beweist keine organische Herkunft.

## Feedback und Prognosen

1. Stündliche Snapshots und verzögert verfügbare Analytics aktualisieren.
2. Fällige Prognosen mit dem ersten Snapshot am/nach Zielzeitpunkt vergleichen, maximal 2 Stunden Toleranz. Ohne passenden Snapshot bleibt die Prognose unausgewertet. Kein Blick in die Zukunft, keine rückwirkende Erfindung eines Zielwerts.
3. Prognosefehler speichern. Pro Video/Horizont werden nicht überlappende Fälle gewählt, um stündliche Prognosen nicht als unabhängige Versuche zu zählen.
4. Kaltstart: heutiger Gesamtzähler + aktuelle Velocity × Horizont. Szenariobereich 0,25–4 × erwarteter Zuwachs; ausdrücklich kein kalibriertes Konfidenzintervall.
5. Ab 10 unabhängigen abgeschlossenen Prognosen: robuster Median-Korrekturfaktor, begrenzt auf 0,1–5.
6. Ab 50 Fällen: Ridge auf logarithmiertem Zuwachs, Missingness-Indikatoren, Velocity, Acceleration, Conversion, Watchtime, Strategieevidenz und Videoalter.
7. Zeitliche 80/20-Aufteilung. Trainingsziele müssen vor dem ersten Validierungsursprung bereits beobachtet worden sein. Ridge wird nur übernommen, wenn sein MAE mindestens 5 % besser als sowohl lineare als auch korrigierte Basisprognose ist.
8. Finale Modellparameter und zugrunde liegende Forecast-IDs werden protokolliert. Keine ausführbaren Pickle-Artefakte nötig.
9. Unsicherheit aus gespeicherten Out-of-sample-Fehlern desselben Modelltyps, Horizonts und Content-Typs. Ab 30 unabhängigen Fällen empirisches 80%-Intervall.
10. Schwellenwahrscheinlichkeiten nur bei mindestens fünf Residualszenarien auf jeder Seite der Schwelle; geglättet als (Treffer+1)/(n+2). Bereits erreichte Schwellen erhalten 1. Andernfalls NULL. Seltene Millionenereignisse können mit kleinem Kanal nicht zuverlässig beziffert werden.

Die Kalibrierung setzt ähnliche zukünftige Daten voraus; sie garantiert weder Stationarität noch unabhängige Zuschauerreaktionen. Die erste echte Million ist mit den Daten eines 600-Abonnenten-Kanals häufig außerhalb des sinnvollen empirischen Bereichs.

## Experiment Memory

Jeder Entwurf enthält Hypothese, erwartetes Ergebnis/Horizont, Content-, Titel-, Thumbnail- und Hookstrategie, Zielgruppe, Design, Kontrollvideo und Störfaktoren. Er kann vor dem Upload ohne Video angelegt werden. Der Messbeginn wird nach Import des veröffentlichten Videos mit aktuellen Snapshots registriert. Hypothese und Ziel sind danach unveränderlich; keine automatische Veröffentlichung.

Ein Vergleich verlangt anderen Kanalinhaber-identischen Content, bekannte gleiche Form und Alterskohorte sowie zeitnahe Kontrollprognose. Ein laufendes Experiment pro Zielvideo verhindert überlappende Zielinterventionen. Die V1 führt keine Zuschauer-Randomisierung aus. Native YouTube-Tests oder sauber randomisierte zukünftige Upload-Experimente benötigen zusätzliche Integration.

Ergebnis:
- zusätzlicher View-Zuwachs,
- absolute und relative Abweichung vom vorab gesetzten Ziel,
- bei Vergleich: Zielabweichung minus relative Abweichung der Kontrollprognose,
- vermutete Ursache (zunächst explizit unbekannt),
- abgeleitete nächste Hypothese,
- Confidence und spätere Interpretationshistorie.

Strategieevidenz gruppiert die vollständige Kombination aller fünf Strategiefelder. Ein Video oder wiederverwendetes Kontrollvideo darf unabhängige Evidenz nicht beliebig vervielfachen. Erfolg bedeutet hier positive vergleichsbereinigte Zielabweichung, nicht bewiesene Kausalwirkung.

Beta(1,1)-Prior, anschließend Beta(1+Erfolge,1+Fehlschläge). Confidence bezeichnet P(Erfolgsrate > 50 %) unter diesem Modell; zusätzlich 95%-Credible-Intervall der Erfolgsrate. Erst ab 10 unabhängigen Vergleichen und unterer Intervallgrenze über 50 % wird „wiederholt vielversprechend“ angezeigt. Auch das ist kein Beweis. Viele parallele Hypothesen können Selektionsverzerrungen erzeugen; vorab begrenzte Testpläne und externe Bestätigung bleiben nötig.

Erfolge und Fehlschläge verändern den geschrumpften Strategie-Featurewert. Dieser fließt in das validierte Ridge-Modell ein. Ohne ausreichende Trainingsdaten bleibt der konservative Basisansatz aktiv. Explorative Beobachtungen bleiben gespeichert und fließen über tatsächliche Prognoseergebnisse in den allgemeinen Forecast-Feedback-Loop ein, erhalten aber keine kontrollierte Strategieevidenz.

## Erweiterungen

`Predictor`, `ExperimentPolicy` und `BayesianOptimizer` definieren die Integrationspunkte. Ein reproduzierbarer Thompson-Sampling-Vorschlag ist als reine Offline-Funktion vorhanden. Kein automatisches Arm-Assignment, keine Titel-/Thumbnail-Updates, kein Engagement-Bot. XGBoost ist optional installierbar, aber in V1 nicht als trainiertes Produktivmodell aktiviert. Bayesian Optimization ist ein Vertrag, noch kein Optimierungslauf. Revenue und langfristiger Zuschauerwert dürfen später eigene Zielgewichte erhalten; aktuell werden fehlende Werte nicht erfunden.
