# Content Intelligence

Die Growth Engine kannte das eigene Material nicht und leitete Shorts allein aus der Retentionskurve ab. Deren Maximum liegt bei einem kurzen Video fast immer am Anfang, also lautete die Empfehlung „Start bei Sekunde 1“ – ein Artefakt, keine Auswahl. Diese Schicht schließt die Lücke: die Anwendung versteht den eigenen Inhalt, indem sie ihn einmalig lokal **messt**.

## Wo was läuft

| Einmalig lokal (`python -m app.cli content`) | Dauerhaft in Neon | Vercel/Cron |
|---|---|---|
| Dekodieren, Struktur, Energie, Wiederholung, Gesangsanteil, Bildschnitte, Helligkeit, optional Text, Rendern | `media_assets`, `content_sections`, `content_lines`, `content_candidates` | liest nur diese Tabellen |

Die Mediendateien bleiben auf der eigenen Maschine und erreichen Vercel nie. Das deployte Paket bekommt dadurch **keine** neue Abhängigkeit: `app/content.py` importiert nichts Mediennahes (von `tests/test_content.py` geprüft), und die Medienwerkzeuge stehen in der optionalen Gruppe `content`, die nur lokal installiert wird. `app/media.py` verweigert in gehosteten Umgebungen den Dienst.

Identität eines Assets ist der SHA-256 der Datei. Eine unveränderte Datei wird nie zweimal analysiert; `--reanalyse` erzwingt eine Neumessung, wenn sich die Messung selbst geändert hat, und verwirft dabei nur noch nicht veröffentlichte Kandidaten.

## Keine erfundenen Labels

Gespeichert werden Messwerte, keine Benennungen: `energy`, `energy_rel`, `repetition_strength`, `repetition_group`, `vocal_presence`, `novelty`, `visual_cuts`, `visual_cut_density`, `brightness`, `boundary_cut_distance`. Wiederholung plus Energie wird **nicht** zu „Refrain“. Die Entscheidung funktioniert ohne diese Benennung, und kein Ausgabefeld enthält sie.

`repetition_strength` misst den Kontrast, nicht die rohe Ähnlichkeit: in einem Musikstück ähneln sich alle Abschnitte stark, der Rohwert sättigt bei 1 und unterscheidet nichts. Gemessen wird, wie viel mehr ein Abschnitt seinem nächsten Verwandten gleicht als dem Durchschnitt des Stücks.

## Mehrere Kandidaten statt eines behaupteten Gewinners

Je Datei entstehen bis zu `MAX_CANDIDATES` Kandidaten mit exakter Start- und Endzeit. Sie müssen sich messbar unterscheiden: mindestens `DISTINCT_SECONDS` Abstand im Startpunkt, höchstens `DISTINCT_OVERLAP` Überlappung und ein erkennbarer Unterschied in mindestens einer Eigenschaft. Der Start liegt auf einem echten Bildschnitt, wenn einer innerhalb von `CUT_SNAP_SECONDS` liegt; das Ende auf einer gemessenen Abschnittsgrenze, sonst an der Shorts-Grenze von 60 Sekunden.

`produce_for_opportunity` ordnet diese Kandidaten aus drei Quellen: gemessene Materialeigenschaften, die Retentionskurve desselben Videos im jeweiligen Fenster und die belegte Audience-/Placement-Chance. Es gibt keinen erfundenen Erwartungswert – nur eine Ordnung aus Faktoren, die alle mit dem Vorschlag mitgeliefert werden.

Material existiert, bevor es ein veröffentlichtes Video gibt: Kandidaten entstehen auch für eine Datei ohne Zuordnung. Fehlt die Retention, steht das in der Evidenz und wird nicht ersetzt.

## Zuordnung Datei → Video

Über die Dauer, mit `DURATION_TOLERANCE` Toleranz, und nur wenn genau ein Video passt. Mehrdeutig heißt ohne Zuordnung, nicht irgendeine Zuordnung.

## Hook nur aus eigenem Wortlaut

Songtexte sind optional. Liegt neben der Datei eine `.txt` mit dem eigenen Text, werden die erkannten Segmente dagegen ausgerichtet; ab `ALIGN_SIMILARITY` gilt eine Zeile als `aligned` und ist zitierfähig. Alles andere bleibt `asr` mit gemessener Sicherheit und wird unterhalb von `HOOK_MIN_CONFIDENCE` **nie** als Hook oder für das Packaging verwendet – maschinelle Erkennung auf gesungenen Vocals erfindet Text. Ohne belegten Wortlaut gibt es keinen Hook, sondern den gemessenen Startpunkt. Gepflegte Zeitmarken gibt es nirgends.

## Lernen aus tatsächlicher Distribution

Nach dem Upload verbindet ein Aufruf den Short mit seinem Kandidaten:

```
python -m app.cli published --candidate <id> --video-id <neue Video-ID>
```

Danach fließen seine eigenen Impressions, Views, Trafficquellen und Retention über den normalen Sync ein – er ist ein eigenes Asset wie jedes andere. `content.priors()` vergleicht je Eigenschaft, ob die Hälfte mit dem höheren Messwert mehr algorithmische Auslieferung (`SHORTS`, `BROWSE`, `SUGGESTED`, ohne Werbetraffic) erhielt, und gewichtet die künftige Auswahl entsprechend.

Unter `MIN_PUBLISHED_FOR_PRIORS` veröffentlichten Shorts bleiben die Gewichte neutral, und der Lernstand sagt das ausdrücklich. Ein oder zwei Shorts sind kein Muster; das wird ausgesprochen statt verdeckt.

## Was menschlich bleibt

Freigabe und Upload. Der YouTube-Zugriff bleibt read-only; das System ändert auf YouTube nichts und kann es nicht. Der Schnitt selbst entsteht automatisch (`--render`, ffmpeg, Hochformat ohne Beschneiden), und eine neue Masterdatei muss einmal im Master-Verzeichnis liegen.

## Grenzen

- Ohne ffmpeg gibt es keine Verarbeitung; ohne librosa keine Struktur. Ein fehlendes optionales Werkzeug lässt genau die Messwerte entfallen, die es liefert – es erzeugt keine Ersatzwerte.
- Eine Datei ohne Audiospur wird nicht analysiert.
- Bildschnitte und Helligkeit gibt es nur bei einer Videospur; für reines Audio entfallen sie.
- `vocal_presence` ist ohne Text ein Energieanteil im Gesangsband (200–4000 Hz) der harmonischen Komponente, kein Stimmerkenner.
