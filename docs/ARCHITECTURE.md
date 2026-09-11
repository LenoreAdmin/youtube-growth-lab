# Architektur und Methoden

Serverless-Erweiterung: [VERCEL.md](VERCEL.md). `jobs.py` übernimmt persistente Leases, `budget.py` das Zeitbudget und `release.py` sichere Build-Migrationen. Vercel Cron ersetzt im gehosteten Betrieb den lokalen Worker. Die zusätzlichen Tabellen sind `job_leases` und `imported_reports` (Migration 0002).

## Module

```text
app/
  config.py        Umgebungsvariablen
  models.py        relationales Datenmodell
  db.py            Engine und Sessions
  youtube.py       offizielle APIs und Desktop-OAuth
  pipeline.py      historische Imports, Snapshots, Feedback-Zyklus
  backfill.py      manueller, resumierbarer historischer Import (eigene Lease)
  history.py       V4 leakage-freier historischer Trainingsdatensatz
  backtest.py      V4 Walk-forward-Backtests, Baselines, Modellwahl
  regimes.py       V4 quantilbasierte Growth-Regime
  strategy.py      V4 Strategy Engine und Experiment-Kontext
  learning.py      V4 Orchestrierung, Feedback-Loop, Dashboard-Overview
  metrics.py       Velocity, Acceleration, Momentum, Conversion
  prediction.py    Baseline, RÃ¼ckkopplung, Ridge und Kalibrierung
  memory.py        Vorregistrierung, Auswertung, Strategieevidenz
  experiments.py   Policy-VertrÃ¤ge und Offline-Thompson-Vorschlag
  main.py          FastAPI, Auth und Dashboard-Endpunkte
  cli.py           oauth / sync / worker / demo
  demo.py          ausschlieÃŸlich synthetische Beispieldaten
  static/          HTML, CSS, JavaScript
migrations/        eingefrorene Alembic-Migration
tests/             Rechen-, Import-, API-, Memory- und Migrationstests
```

Keine zusÃ¤tzliche Node-Laufzeit im Betrieb. Diese Entscheidung hÃ¤lt eine interne Single-Owner-V1 einfach; die REST-API erlaubt spÃ¤ter ein separates Frontend.

## Datenbank

| Tabelle | Zweck / SchlÃ¼ssel |
|---|---|
| channels | Autorisierter Kanal und aktueller Abostand |
| videos | YouTube-ID, Upload-Zeitpunkt, LÃ¤nge, Aktivstatus |
| snapshots | Kumulative ZÃ¤hler; UNIQUE(video_id, observed_at), UTC |
| video_daily | Historische Analytics-Tageswerte; PK(video_id, day) |
| reports | Typ, Zeitraum, Zeilen und Abrufzeit; Retention, Traffic, Revenue |
| reach_daily | Offizielle Thumbnail-Impressionen und CTR je Video/Tag |
| ingest_cursors | Letzter erfolgreich abgefragter Zeitraum je Video/Art |
| sync_runs | Laufstatus, Zeitstempel, isolierte Fehler |
| backfill_runs / backfill_progress | Manuelle historische Importläufe und resumierbare Cursor je Video/Art ([BACKFILL.md](BACKFILL.md)) |
| video_traffic_daily | Historische Traffic-Quellen je Video/Tag; `paid` markiert Werbetraffic |
| backfill_reports | Historische Retention je Kalendermonat mit exaktem Fenster |
| learning_datasets / learning_backtests | V4: signierte historische Datensätze, Audit, Walk-forward-Backtests je Horizont ([V4.md](V4.md)) |
| analytics_forecasts | V4: Analytics-Prognosen mit damaligen Features, Baseline, Intervall und späterem Ausgang |
| strategy_recommendations | V4: versionierte Handlungsempfehlungen je Video/Tag mit Forecast-/Experiment-Links |
| forecasts | UnverÃ¤nderliche Vorhersage, Features, Modell, tatsÃ¤chliches Ergebnis |
| model_runs | Validierung, Modellparameter, Trainings-IDs, Koeffizienten/Skalierung |
| decisions | VollstÃ¤ndiges Experiment Memory, vorab definierte Strategie |
| memory_reviews | NachtrÃ¤gliche Interpretationen append-only |
| experiments | Erweiterung fÃ¼r mehrarmige ExperimentplÃ¤ne |

FremdschlÃ¼ssel und eindeutige SchlÃ¼ssel verhindern verwaiste bzw. doppelte Daten. PostgreSQL ist das Produktionsziel; SQLite dient reproduzierbaren Tests. SchemaÃ¤nderungen erhalten kÃ¼nftig neue Migrationen; 0001 ist unabhÃ¤ngig von spÃ¤teren Modelldefinitionen eingefroren.

Snapshots sind aktuelle Data-API-Messungen. Historische Snapshots werden nicht aus Tageswerten erfunden. Analytics-Tage folgen der YouTube-Berichtszeitzone America/Los_Angeles. Snapshots und Prognosezeitpunkte sind UTC. Der aktuelle Tagesbereich wird mit Sicherheitsabstand abgefragt; die letzten 30 Tage werden erneut geladen. Leere API-Berichte bleiben fehlende Beobachtungen und werden nicht als gemessene Nullen ausgegeben.

Historische Tagesberichte werden in 180-Tage-Fenstern paginiert. Erfolgreiche Fenster werden transaktional ersetzt. Fehler belassen bisherige Daten. Upload-Playlist-Pagination vermeidet teure Suchanfragen. Report-Reimporte ersetzen Werte anstatt sie aufzusummieren. CTR wird bei mehreren Segmenten impressionsgewichtet.

## Metriken

- View Velocity: Differenz des GesamtzÃ¤hlers / tatsÃ¤chlich vergangene Stunden.
- Growth Acceleration: Differenz zweier View-Velocities / Abstand der Fenstermittelpunkte; Einheit Views/hÂ².
- Verwendet werden mÃ¶glichst zwei 24h-Fenster; Intervalle unter 1h oder Ã¼ber 36h werden verworfen. Korrekturen nach unten in irgendeinem Zwischenintervall invalidieren die Berechnung.
- Subscriber Conversion: gewonnene Abonnenten / Analytics-Views im selben Zeitfenster.
- Watchtime Efficiency: Watchtime in Sekunden / Views / VideolÃ¤nge. Das ist die hier definierte normalisierte Watchtime, nicht automatisch dieselbe GrÃ¶ÃŸe wie YouTubes durchschnittlicher Prozentwert. Wiederholungen kÃ¶nnen Werte Ã¼ber 1 erzeugen. Der offizielle Durchschnitt bleibt zusÃ¤tzlich in den Tagesdaten erhalten.
- Relative Performance: aktuelle Velocity / Durchschnitt anderer Videos desselben bekannten Content-Typs und derselben groben Alterskohorte (0â€“29, 30â€“59, 60â€“89, 90+ Tage).
- Momentum: 50 + 50 Ã— gewichtete Kombination aus tanh(Velocity-Trend) und tanh(log2(relative Performance)); Gewichte 65/35. Ohne geeignete Vergleichsvideos wird das verfÃ¼gbare Gewicht renormalisiert. Ohne verlÃ¤ssliche Snapshots kein Score.
- Die Heuristik bewertet Wachstum und wird noch nicht als wissenschaftlich validierter Erfolgsindikator bezeichnet.
- UmsÃ¤tze separat; RPM = Umsatz / Views desselben Revenue-Berichts Ã— 1000, USD. Unbekannt bleibt NULL, nicht 0.

Beobachtete Traffic-AnteilsÃ¤nderungen ab 10 Prozentpunkten werden als mÃ¶gliche Faktoren genannt. CTR und Retention sind sichtbar, jedoch ohne ausreichendes Experiment kein Kausalnachweis. Bei Werbe-Traffic werden GesamtzÃ¤hler-Prognosen und Score ausgesetzt. Nicht als Werbung klassifizierte Views werden separat gezeigt; ein leerer Bericht beweist keine organische Herkunft.

## Feedback und Prognosen

1. StÃ¼ndliche Snapshots und verzÃ¶gert verfÃ¼gbare Analytics aktualisieren.
2. FÃ¤llige Prognosen mit dem ersten Snapshot am/nach Zielzeitpunkt vergleichen, maximal 2 Stunden Toleranz. Ohne passenden Snapshot bleibt die Prognose unausgewertet. Kein Blick in die Zukunft, keine rÃ¼ckwirkende Erfindung eines Zielwerts.
3. Prognosefehler speichern. Pro Video/Horizont werden nicht Ã¼berlappende FÃ¤lle gewÃ¤hlt, um stÃ¼ndliche Prognosen nicht als unabhÃ¤ngige Versuche zu zÃ¤hlen.
4. Kaltstart: heutiger GesamtzÃ¤hler + aktuelle Velocity Ã— Horizont. Szenariobereich 0,25â€“4 Ã— erwarteter Zuwachs; ausdrÃ¼cklich kein kalibriertes Konfidenzintervall.
5. Ab 10 unabhÃ¤ngigen abgeschlossenen Prognosen: robuster Median-Korrekturfaktor, begrenzt auf 0,1â€“5.
6. Ab 50 FÃ¤llen: Ridge auf logarithmiertem Zuwachs, Missingness-Indikatoren, Velocity, Acceleration, Conversion, Watchtime, Strategieevidenz und Videoalter.
7. Zeitliche 80/20-Aufteilung. Trainingsziele mÃ¼ssen vor dem ersten Validierungsursprung bereits beobachtet worden sein. Ridge wird nur Ã¼bernommen, wenn sein MAE mindestens 5 % besser als sowohl lineare als auch korrigierte Basisprognose ist.
8. Finale Modellparameter und zugrunde liegende Forecast-IDs werden protokolliert. Keine ausfÃ¼hrbaren Pickle-Artefakte nÃ¶tig.
9. Unsicherheit aus gespeicherten Out-of-sample-Fehlern desselben Modelltyps, Horizonts und Content-Typs. Ab 30 unabhÃ¤ngigen FÃ¤llen empirisches 80%-Intervall.
10. Schwellenwahrscheinlichkeiten nur bei mindestens fÃ¼nf Residualszenarien auf jeder Seite der Schwelle; geglÃ¤ttet als (Treffer+1)/(n+2). Bereits erreichte Schwellen erhalten 1. Andernfalls NULL. Seltene Millionenereignisse kÃ¶nnen mit kleinem Kanal nicht zuverlÃ¤ssig beziffert werden.

Die Kalibrierung setzt Ã¤hnliche zukÃ¼nftige Daten voraus; sie garantiert weder StationaritÃ¤t noch unabhÃ¤ngige Zuschauerreaktionen. Die erste echte Million ist mit den Daten eines 600-Abonnenten-Kanals hÃ¤ufig auÃŸerhalb des sinnvollen empirischen Bereichs.

## Experiment Memory

Jeder Entwurf enthÃ¤lt Hypothese, erwartetes Ergebnis/Horizont, Content-, Titel-, Thumbnail- und Hookstrategie, Zielgruppe, Design, Kontrollvideo und StÃ¶rfaktoren. Er kann vor dem Upload ohne Video angelegt werden. Der Messbeginn wird nach Import des verÃ¶ffentlichten Videos mit aktuellen Snapshots registriert. Hypothese und Ziel sind danach unverÃ¤nderlich; keine automatische VerÃ¶ffentlichung.

Ein Vergleich verlangt anderen Kanalinhaber-identischen Content, bekannte gleiche Form und Alterskohorte sowie zeitnahe Kontrollprognose. Ein laufendes Experiment pro Zielvideo verhindert Ã¼berlappende Zielinterventionen. Die V1 fÃ¼hrt keine Zuschauer-Randomisierung aus. Native YouTube-Tests oder sauber randomisierte zukÃ¼nftige Upload-Experimente benÃ¶tigen zusÃ¤tzliche Integration.

Ergebnis:
- zusÃ¤tzlicher View-Zuwachs,
- absolute und relative Abweichung vom vorab gesetzten Ziel,
- bei Vergleich: Zielabweichung minus relative Abweichung der Kontrollprognose,
- vermutete Ursache (zunÃ¤chst explizit unbekannt),
- abgeleitete nÃ¤chste Hypothese,
- Confidence und spÃ¤tere Interpretationshistorie.

Strategieevidenz gruppiert die vollstÃ¤ndige Kombination aller fÃ¼nf Strategiefelder. Ein Video oder wiederverwendetes Kontrollvideo darf unabhÃ¤ngige Evidenz nicht beliebig vervielfachen. Erfolg bedeutet hier positive vergleichsbereinigte Zielabweichung, nicht bewiesene Kausalwirkung.

Beta(1,1)-Prior, anschlieÃŸend Beta(1+Erfolge,1+FehlschlÃ¤ge). Confidence bezeichnet P(Erfolgsrate > 50 %) unter diesem Modell; zusÃ¤tzlich 95%-Credible-Intervall der Erfolgsrate. Erst ab 10 unabhÃ¤ngigen Vergleichen und unterer Intervallgrenze Ã¼ber 50 % wird â€žwiederholt vielversprechendâ€œ angezeigt. Auch das ist kein Beweis. Viele parallele Hypothesen kÃ¶nnen Selektionsverzerrungen erzeugen; vorab begrenzte TestplÃ¤ne und externe BestÃ¤tigung bleiben nÃ¶tig.

Erfolge und FehlschlÃ¤ge verÃ¤ndern den geschrumpften Strategie-Featurewert. Dieser flieÃŸt in das validierte Ridge-Modell ein. Ohne ausreichende Trainingsdaten bleibt der konservative Basisansatz aktiv. Explorative Beobachtungen bleiben gespeichert und flieÃŸen Ã¼ber tatsÃ¤chliche Prognoseergebnisse in den allgemeinen Forecast-Feedback-Loop ein, erhalten aber keine kontrollierte Strategieevidenz.

## Erweiterungen

`Predictor`, `ExperimentPolicy` und `BayesianOptimizer` definieren die Integrationspunkte. Ein reproduzierbarer Thompson-Sampling-Vorschlag ist als reine Offline-Funktion vorhanden. Kein automatisches Arm-Assignment, keine Titel-/Thumbnail-Updates, kein Engagement-Bot. XGBoost ist optional installierbar, aber in V1 nicht als trainiertes Produktivmodell aktiviert. Bayesian Optimization ist ein Vertrag, noch kein Optimierungslauf. Revenue und langfristiger Zuschauerwert dÃ¼rfen spÃ¤ter eigene Zielgewichte erhalten; aktuell werden fehlende Werte nicht erfunden.
