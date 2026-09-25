"""Audience-Intents: wonach das Publikum unseres Videos sucht – belegt aus unseren eigenen Daten.

Warum es diese Schicht gibt: die Pool-Suche hat vorher einzelne Tags oder Titelwörter als Query benutzt.
Daraus wurden „11am album teaser“ (K-Pop, Kirche, Kodak Black) und „good times“ (TV-Serie). Ein Tag ist
kein Suchintent. Ein Intent ist eine belegte Kombination aus einem Thema und seinem Kontext – Genre,
Musik, Stimmung oder ein Künstler, neben dem YouTube uns nachweislich ausliefert.

Zwei Regeln, die hier nicht verhandelbar sind:

1. Nichts wird erfunden. Jeder Begriff muss in unseren eigenen Daten stehen: eigener Titel, eigene
   Beschreibung, eigene Tags, YouTube-Themenkategorien des eigenen Videos oder Kanals, reale eigene
   Suchbegriffe aus den Analytics, oder die Nachbarschaft, aus der messbar Zuschauer kamen. Jeder
   Intent trägt seine Belege mit sich und zeigt sie im Dashboard.
2. Die Kategorie eines Begriffs entscheidet, ob er ein Audience-Thema sein darf. Marken- und
   Release-Namen („sealand“, „trainstories“, „11am“) und Formatwörter („album“, „teaser“, „official“)
   beschreiben unsere Verpackung, nicht das Interesse fremder Zuschauer. Sie taugen als Kontext, nie
   als Themenkopf. Die Wortlisten unten klassifizieren nur, was ohnehin aus unseren Daten kommt; sie
   fügen kein Thema hinzu.
"""
import re
from collections import defaultdict
from sqlalchemy import select
from .models import AudiencePool, DiscoveryItem, DiscoverySignal, Video, VideoProfile

# Genres und Stile. Ein Begriff zählt nur, wenn er in unseren Daten vorkommt – die Liste sagt nur,
# in welche Schublade er dann gehört.
# Wörter aus Beschreibungstexten, die nichts über die Zielgruppe sagen: Aufrufe an die Zuschauer,
# Verweise auf eigene Seiten, Credits-Vokabular, Bindewörter und Satzfragmente. Genau daraus entstanden
# „webpage pop“, „released pop“, „check rock“, „unser rock“ und „musicvideo pop“.
HELPER_WORDS = {
    # Verweise und Aufrufe
    "webpage", "website", "homepage", "page", "seite", "link", "links", "click", "klick", "subscribe",
    "abonnieren", "abo", "follow", "folge", "listen", "hoeren", "hören", "stream", "streaming", "download",
    "available", "verfuegbar", "verfügbar", "shop", "merch", "store", "kaufen", "buy", "order", "spotify",
    "apple", "deezer", "tidal", "instagram", "facebook", "tiktok", "twitter", "socials", "kanal", "channel",
    # Credits und Produktion
    "credits", "produced", "produktion", "production", "produzent", "prod", "mixed", "mixing", "master",
    "mastered", "mastering", "recorded", "aufnahme", "aufgenommen", "written", "composed", "komposition",
    "arrangement", "regie", "directed", "director", "kamera", "camera", "schnitt", "editing", "artwork",
    "design", "grafik", "foto", "photo", "photography", "label", "booking", "management", "rights",
    "copyright", "reserved", "verlag", "publishing", "musicvideo", "videoclip",
    # Wertende und zaehlende Fuellwoerter: sie sagen nichts ueber ein Publikum aus. „Long Ride“ teilte
    # mit uns genau solche Woerter.
    "good", "best", "better", "great", "nice", "beautiful", "amazing", "awesome", "favorite", "favourite",
    "times", "time", "long", "short", "little", "full", "part", "thing", "things", "way", "ways", "real",
    "true", "free", "easy", "hard", "top", "big", "small", "old", "young", "high", "low", "last", "next",
    # Satzfragmente und Fuellwoerter
    "released", "release", "out", "now", "new", "neu", "neue", "neuer", "neues", "from", "with", "this",
    "that", "here", "hier", "check", "schau", "unser", "unsere", "unseren", "meine", "mein", "our", "your",
    "you", "ich", "wir", "uns", "dir", "euch", "special", "debut", "first", "erste", "erstes", "second",
    "all", "alle", "more", "mehr", "about", "ueber", "über", "thanks", "danke", "please", "bitte", "enjoy",
    "watch", "sehen", "gesehen", "made", "gemacht", "gibt", "kommt", "wurde", "wird", "sind", "haben",
    "hat", "war", "waren", "sein", "seine", "sowie", "auch", "noch", "schon", "immer", "wieder", "sehr",
    "ganz", "viel", "viele", "nach", "vor", "bei", "aus", "auf", "durch", "ohne", "gegen", "zwischen",
    "jetzt", "heute", "morgen", "gestern", "dann", "wenn", "aber", "oder", "denn", "weil", "dass"}
# Zeilen mit diesen Markern sind Credits oder Kontaktangaben: dort stehen Namen von Menschen und Firmen,
# keine Themen. Ein Name wie „Factoria“ oder „Sanchez“ ist keine Zielgruppe.
CREDIT_MARKERS = ("musik", "music by", "text", "lyrics", "prod", "produ", "mix", "master", "recorded",
                  "aufnahme", "kamera", "camera", "video by", "regie", "directed", "written", "composed",
                  "artwork", "design", "foto", "photo", "label", "booking", "management", "copyright",
                  "credits", "mastering", "mixing", "schnitt", "grafik", "cover by", "feat", "with ")
URL_PATTERN = re.compile(r"https?://\S+|www\.\S+|\b[\w-]{2,}\.(?:ch|com|de|net|org|io|me|fm|tv|to|at|uk|eu)\b",
                         re.IGNORECASE)
CONTACT_PATTERN = re.compile(r"[@#]\S+|\S+@\S+\.\S+")

# „music“ und Verwandtes ordnen ein Thema ein, sind aber selbst keines: nach „music“ zu suchen liefert
# die halbe Plattform. Diese Woerter duerfen nur Kontext sein, niemals Themenkopf.
MUSIC_WORDS = {"music", "musik", "song", "songs", "sound", "sounds", "audio", "playlist", "mixtape"}
GENRE_WORDS = {"ambient", "lofi", "lo-fi", "chillout", "chill", "downtempo", "instrumental", "acoustic",
               "orchestral", "cinematic", "soundtrack", "score", "electronic", "electronica", "synth",
               "synthwave", "techno", "house", "trance", "folk", "indie", "rock", "pop", "jazz", "blues",
               "classical", "piano", "guitar", "choir", "gospel", "worship", "hymn", "soul", "funk",
               "reggae", "hiphop", "rap", "metal", "punk", "country", "world", "ethno", "meditation"}
# Stimmung und Anlass: wofür Zuschauer solche Musik suchen.
MOOD_WORDS = {"relax", "relaxing", "calm", "calming", "sleep", "sleeping", "focus", "study", "studying",
              "work", "working", "reading", "dream", "dreamy", "melancholy", "melancholic", "nostalgia",
              "nostalgic", "cozy", "peaceful", "quiet", "slow", "night", "nightly", "morning", "sunset",
              "rain", "rainy", "winter", "summer", "autumn", "driving", "walking", "hiking", "workout",
              "healing", "mindful", "mindfulness", "background", "atmosphere", "atmospheric"}
# Ort, Reise, Kultur: das Motiv, um das herum ein Publikum existiert.
TRAVEL_WORDS = {"train", "trains", "railway", "railways", "rail", "station", "locomotive", "wagon",
                "journey", "journeys", "route", "trip", "travel", "traveling", "travelling", "voyage",
                "road", "highway", "flight", "ship", "ferry", "transsib", "transsiberian", "siberian",
                "mongolian", "mongolia", "steppe", "desert", "mountain", "mountains", "sea", "ocean",
                "river", "forest", "city", "village", "landscape", "scenery", "nature"}
# Release- und Zeitmarken: „11am“, „vol 2“, „part 3“ beschreiben eine Veröffentlichung, kein Interesse.
RELEASE_PATTERN = re.compile(r"^(\d+(am|pm|st|nd|rd|th)?|vol|volume|part|pt|ep|episode|no|nr|track|side)\d*$")
# Woerter, die nur die Herkunft einer Aufnahme bezeichnen. „Topic“ steht in automatisch erzeugten
# YouTube-Kuenstlerkanaelen („Melissa Lischer - Topic“) und beschreibt kein Interesse.
LABEL_WORDS = {"topic", "official", "oficial", "records", "recordings", "audio", "lyrics", "remaster",
               "remastered", "live", "version", "edit", "mix", "radio", "feat", "band", "channel"}
OWN_SOURCES = ("own_title", "own_description", "own_tag", "own_topic", "own_search_term",
               "channel_topic", "channel_keywords", "channel_description")

TOPIC_CATEGORIES = ("thema", "ort", "genre", "mood")     # Kategorien, die einen Intent tragen dürfen
HEAD_CATEGORIES = ("thema", "ort")                       # Kategorien, die Themenkopf sein dürfen
CONTEXT_CATEGORIES = ("genre", "mood")                   # Kategorien, die einen Kopf einordnen
MIN_ATTESTATIONS = 2          # Zwei unabhängige eigene Quellen, sonst bleibt es eine Vermutung.
MAX_INTENTS_PER_VIDEO = 4
# Ein eigenes Thema mit Kontext ist die stärkste Spur; ein Künstler aus der Nachbarschaft ist eine echte,
# aber schmalere Fläche; Stimmung plus Genre trägt nur, wenn es sonst kein Thema gibt.
KIND_BONUS = {"topic_context": 2.0, "search_demand": 2.0, "artist_adjacency": 0.5, "mood_genre": 0.0}
MAX_ARTIST_INTENTS = 1        # Ein Nachbarschafts-Künstler je Video genügt; sonst verdrängt er das Thema.
MIN_HEAD_LENGTH = 4           # Kürzere Wörter tragen kein Thema.
RETIRE_AFTER_CANDIDATES = 6   # So viele Kandidaten ohne einen einzigen Treffer, dann ist der Intent tot.

SOURCE_LABELS = {
    "own_title": "Eigener Titel",
    "own_description": "Eigene Videobeschreibung",
    "own_tag": "Eigener Video-Tag",
    "own_topic": "YouTube-Themenkategorie des eigenen Videos",
    "channel_topic": "YouTube-Themenkategorie des eigenen Kanals",
    "channel_keywords": "Kanal-Schlüsselwörter",
    "channel_description": "Kanalbeschreibung",
    "own_search_term": "Realer Suchbegriff aus den eigenen Analytics",
    "neighbour_title": "Titel eines Videos, das uns messbar Zuschauer geschickt hat",
    "neighbour_tag": "Tag eines solchen Nachbarvideos",
    "neighbour_artist": "Kanal, neben dem YouTube uns messbar ausliefert",
}


def _tokens(text):
    from .discovery import tokens as split
    return split(text or "")


def topic_words(urls):
    """Aus „https://en.wikipedia.org/wiki/Ambient_music“ wird {ambient, music}."""
    words = set()
    for url in urls or []:
        tail = str(url).rstrip("/").rsplit("/", 1)[-1]
        words.update(w for w in _tokens(tail.replace("_", " ")) if len(w) >= 3)
    return words


def description_words(text):
    """Nur die inhaltlichen Wörter einer Beschreibung – ohne Links, Kontakte und Credits-Zeilen.

    Eine Videobeschreibung besteht zum groessten Teil aus Verweisen und Credits. „Musik: Sanchez“,
    „Webpage: …“ oder „Musicvideo: Factoria“ nennen Menschen, Firmen und Seiten. Wer das mitliest,
    haelt Personennamen fuer Themen – genau das ist passiert.
    """
    kept = []
    for line in re.split(r"[\n\r]+", text or ""):
        clean = CONTACT_PATTERN.sub(" ", URL_PATTERN.sub(" ", line))
        lowered = clean.lower().strip()
        if not lowered:
            continue
        if any(marker in lowered for marker in CREDIT_MARKERS):
            continue        # Credits-Zeile: enthaelt Namen, keine Themen.
        if ":" in clean and len(clean.split(":")[0].split()) <= 3:
            continue        # „Label: X“, „Kamera: Y“ – dieselbe Bauform ohne bekanntes Stichwort.
        kept.append(clean)
    return _tokens(" ".join(kept))


def collect_terms(session, video):
    """Alle Begriffe über dieses Video, mit Quelle und – wo vorhanden – gemessenen Views."""
    terms = defaultdict(lambda: {"sources": {}, "views": 0, "phrases": set()})

    def add(term, source, detail, views=0, phrase=None):
        if not term or len(term) < 3:
            return
        entry = terms[term]
        entry["sources"].setdefault(source, detail)
        entry["views"] += views
        if phrase:
            entry["phrases"].add(phrase)

    profile = session.get(VideoProfile, video.id)
    for word in _tokens(video.title):
        add(word, "own_title", video.title)
    if profile is not None:
        for word in description_words(profile.description)[:120]:
            add(word, "own_description", (profile.description or "")[:160])
        for tag in (profile.tags or [])[:30]:
            phrase = " ".join(_tokens(tag))
            for word in _tokens(tag):
                add(word, "own_tag", tag, phrase=phrase if len(phrase.split()) > 1 else None)
        for word in topic_words(profile.topics):
            add(word, "own_topic", ", ".join(profile.topics or []))
        # YouTube fuehrt das Video selbst unter einer Musikkategorie: „music“ ist damit belegt und nicht geraten.
        if any("music" in str(url).lower() for url in (profile.topics or [])+(profile.channel_topics or [])):
            add("music", "own_topic", ", ".join((profile.topics or [])+(profile.channel_topics or [])))
        for word in topic_words(profile.channel_topics):
            add(word, "channel_topic", ", ".join(profile.channel_topics or []))
        for word in _tokens(profile.channel_keywords)[:40]:
            add(word, "channel_keywords", (profile.channel_keywords or "")[:160])
        for word in _tokens(profile.channel_description)[:60]:
            add(word, "channel_description", (profile.channel_description or "")[:160])
    for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.video_id == video.id,
                                                             DiscoverySignal.kind == "own_search_term")):
        phrase = " ".join(_tokens(row.detail))
        for word in _tokens(row.detail):
            add(word, "own_search_term", row.detail, views=row.views or 0,
                phrase=phrase if len(phrase.split()) > 1 else None)
    neighbours = {row.video_id: row for row in session.scalars(select(DiscoveryItem))}
    for row in session.scalars(select(DiscoverySignal).where(DiscoverySignal.video_id == video.id,
                                                             DiscoverySignal.kind == "own_suggested_source")):
        item = neighbours.get(row.detail)
        if item is None or (row.views or 0) < 1:
            continue
        for word in _tokens(item.title):
            add(word, "neighbour_title", item.title, views=row.views)
        for tag in (item.tags or [])[:10]:
            for word in _tokens(tag):
                add(word, "neighbour_tag", tag, views=row.views)
        artist = " ".join(_tokens(item.channel_title))
        for word in _tokens(item.channel_title):
            add(word, "neighbour_artist", item.channel_title, views=row.views,
                phrase=artist if len(artist.split()) > 1 else None)
    return terms


def classify(term, brand, release, generic, sources=()):
    """Welche Rolle spielt dieser Begriff – und darf er ein Audience-Thema sein?"""
    if term in brand:
        return "marke"
    if term in LABEL_WORDS or term in HELPER_WORDS:
        return "format"
    if term in GENRE_WORDS or term in MUSIC_WORDS:
        return "genre"
    if term in MOOD_WORDS:
        return "mood"
    if term in TRAVEL_WORDS:
        return "ort"
    if term in release or RELEASE_PATTERN.match(term):
        return "release"
    if sources and not any(source in OWN_SOURCES for source in sources):
        # Der Begriff steht ausschliesslich in einem fremden Kanal- oder Videotitel: das ist deren Name,
        # nicht unser Thema. Als Bestaetigung eines eigenen Begriffs zaehlt er weiter, als Themenkopf nicht.
        return "marke"
    if term in generic:
        return "format"
    return "thema"


def profile_terms(session, video, generic=None):
    """Das semantische Profil eines eigenen Videos: Begriff, Kategorie, Belege, Stärke."""
    from .acquisition import generic_tokens
    from .discovery import BRAND
    generic = generic_tokens(session) if generic is None else generic
    terms = collect_terms(session, video)
    frequency = document_frequency(session)
    profile = session.get(VideoProfile, video.id)
    brand = set(BRAND) | set(_tokens(profile.channel_title if profile else ""))
    # Der eigene Titel benennt die Veroeffentlichung, nicht das Interesse fremder Zuschauer. Ein Titelwort
    # wird nur dann zum Thema, wenn es von aussen bestaetigt ist: in einem Nachbartitel, dessen Zuschauer
    # messbar zu uns kamen, oder in einem realen Suchbegriff aus den Analytics.
    outside = {"neighbour_title", "neighbour_tag", "own_search_term", "own_topic", "channel_topic"}
    release = {term for term, data in terms.items()
               if "own_title" in data["sources"] and not (set(data["sources"]) & outside)}
    rows = []
    for term, data in sorted(terms.items()):
        sources = list(data["sources"])
        category = classify(term, brand, release, generic, sources)
        rows.append({"term": term, "category": category, "attestations": len(sources), "sources": sources,
                     "views": data["views"], "phrases": sorted(data["phrases"]), "df": frequency.get(term, 0),
                     "evidence": [{"source": source, "label": SOURCE_LABELS.get(source, source),
                                   "detail": str(detail)[:160]} for source, detail in data["sources"].items()]})
    return rows


def document_frequency(session):
    """Wie oft steht ein Begriff in fremden Titeln und Tags? Haeufig heisst unspezifisch."""
    counts = defaultdict(int)
    for item in session.scalars(select(DiscoveryItem)):
        for word in set(_tokens(item.title)) | {w for tag in (item.tags or [])[:10] for w in _tokens(tag)}:
            counts[word] += 1
    return counts


def intent_history(session):
    """Was ein Query in früheren Läufen gebracht hat – Lernen an echten Ergebnissen, ohne neue Tabelle."""
    history = defaultdict(lambda: {"candidates": 0, "kept": 0})
    for pool in session.scalars(select(AudiencePool)):
        if not pool.query:
            continue
        entry = history[pool.query]
        entry["candidates"] += 1
        if ((pool.details or {}).get("verdict") or {}).get("kept"):
            entry["kept"] += 1
    return dict(history)


def build_intents(session, video, generic=None, history=None):
    """Aus dem Profil werden Suchintents: Themenkopf plus belegter Kontext.

    Ein Intent braucht einen Kopf aus der Kategorie Thema oder Ort und einen Kontext aus Genre, Mood
    oder Musik. Beides muss belegt sein. Zusätzlich gibt es Intents aus belegter Nachbarschaft: eine
    Playlist, in der ein Künstler liegt, neben dem YouTube uns ausliefert, ist eine echte Fläche.
    """
    rows = profile_terms(session, video, generic)
    history = intent_history(session) if history is None else history
    by_term = {row["term"]: row for row in rows}
    heads, contexts = [], []
    for row in rows:
        if row["category"] not in HEAD_CATEGORIES or len(row["term"]) < MIN_HEAD_LENGTH:
            if row["category"] in CONTEXT_CATEGORIES:
                contexts.append(row)
            continue
        # Ein Begriff ohne erkennbare Kategorie (Kategorie „thema“) kann ein Satzfragment sein. Als
        # Themenkopf taugt er nur, wenn ihn mindestens zwei unabhaengige eigene Quellen tragen.
        if row["category"] == "thema" and row["attestations"] < MIN_ATTESTATIONS:
            continue
        heads.append(row)
    contexts.sort(key=lambda r: (-r["attestations"], -r["views"], r["df"], r["term"]))
    heads.sort(key=lambda r: (-r["attestations"], -(1 if r["phrases"] else 0), r["df"], -r["views"], r["term"]))
    intents = []

    def evidence_of(rows_in):
        seen, out = set(), []
        for row in rows_in:
            for item in row["evidence"]:
                key = (item["source"], item["detail"])
                if key in seen:
                    continue
                seen.add(key)
                out.append({**item, "term": row["term"]})
        return out

    def add(kind, head_terms, context_terms, label, rows_in, head_rows=None):
        head = [t for t in head_terms if t]
        if not head:
            return
        query = " ".join(head+[t for t in context_terms if t not in head])[:120]
        key = f"{video.id}:{' '.join(head)}"
        if any(i["key"] == key for i in intents):
            return
        past = history.get(query) or history.get(" ".join(head)) or {}
        head_rows = head_rows or [row for row in rows_in if row["term"] in head]
        context_rows = [row for row in rows_in if row["term"] not in head]
        attestations = min([max((row["attestations"] for row in head_rows), default=1)]
                           + [row["attestations"] for row in context_rows])
        specificity = max((row["df"] for row in head_rows), default=0)
        dead = past.get("candidates", 0) >= RETIRE_AFTER_CANDIDATES and not past.get("kept")
        intents.append({
            "key": key, "kind": kind, "video_id": video.id, "label": label,
            "head": head, "context": [t for t in context_terms if t not in head], "query": query,
            "attestations": attestations, "views": sum(row["views"] for row in rows_in),
            "specificity": specificity,
            "evidence": evidence_of(rows_in), "history": past, "retired": dead,
            "score": round(attestations*2+min(sum(row["views"] for row in rows_in), 20)*0.1
                           + (2 if len(head) > 1 else 0)+(4 if specificity == 0 else (2 if specificity <= 2 else 0))
                           + min(max(len(t) for t in head), 12)*0.1
                           + KIND_BONUS.get(kind, 0)-(5 if dead else 0), 2)})

    # 1. Belegte Nachbarschaft: der Künstler, neben dem wir am stärksten ausgeliefert werden.
    artists = 0
    for row in sorted(rows, key=lambda r: (-r["views"], r["term"])):
        if "neighbour_artist" not in row["sources"] or row["views"] < 1 or artists >= MAX_ARTIST_INTENTS:
            continue
        phrase = next((p for p in row["phrases"] if row["term"] in p.split()), None)
        if not phrase:
            continue
        before = len(intents)
        add("artist_adjacency", phrase.split(), [], f"Publikum von „{phrase}“ (belegte Nachbarschaft)",
            [by_term[t] for t in phrase.split() if t in by_term])
        artists += len(intents) > before

    # 2. Thema/Ort plus Kontext: das eigentliche Audience-Interesse.
    for head in heads:
        phrase = next((p for p in head["phrases"] if len(p.split()) > 1), None)
        head_terms = phrase.split() if phrase else [head["term"]]
        rows_in = [by_term[t] for t in head_terms if t in by_term] or [head]
        carries_style = any(by_term[t]["category"] in CONTEXT_CATEGORIES for t in head_terms if t in by_term)
        if carries_style and len(head_terms) >= 2:
            # Der Kopf nennt Stil und Thema schon selbst („swiss rock“): ein weiteres Genre verwaessert ihn nur.
            add("topic_context", head_terms, [], f"„{' '.join(head_terms)}“", rows_in)
            continue
        context = next((c["term"] for c in contexts if c["term"] not in head_terms), None)
        if context is None:
            continue
        label = f"„{' '.join(head_terms)}“ + {by_term[context]['category']} „{context}“"
        add("topic_context", head_terms, [context], label, rows_in+[by_term[context]])

    # 2b. Stimmung plus Genre: wofür man solche Musik hört. Trägt nur, wenn beides belegt ist.
    moods = [c for c in contexts if c["category"] == "mood"]
    genres = [c for c in contexts if c["category"] == "genre" and c["term"] != "music"]
    if moods and genres:
        add("mood_genre", [moods[0]["term"]], [genres[0]["term"]],
            f"Stimmung „{moods[0]['term']}“ + genre „{genres[0]['term']}“", [moods[0], genres[0]],
            head_rows=[moods[0]])

    # 3. Reale eigene Suchnachfrage: bereits bewiesene Nachfrage, ohne Themenkopf-Pflicht.
    for row in sorted(rows, key=lambda r: -r["views"]):
        if "own_search_term" not in row["sources"] or row["category"] in ("marke", "release", "format"):
            continue
        phrase = next((p for p in row["phrases"] if row["term"] in p.split()), None)
        if phrase:
            add("search_demand", phrase.split(), [], f"Gemessene Suchnachfrage „{phrase}“",
                [by_term[t] for t in phrase.split() if t in by_term])

    ranked = sorted([i for i in intents if not i["retired"]],
                    key=lambda i: (-i["score"], i["specificity"], -len(i["head"])))
    return ranked[:MAX_INTENTS_PER_VIDEO]


def intents(session, videos=None, generic=None):
    """Alle Audience-Intents, nach Belegstärke geordnet – die Grundlage jeder Pool-Suche."""
    videos = list(session.scalars(select(Video).where(Video.active.is_(True)))) if videos is None else videos
    history = intent_history(session)
    from .acquisition import generic_tokens
    generic = generic_tokens(session) if generic is None else generic
    per_video = {}
    for video in videos:
        rows = build_intents(session, video, generic, history)
        if rows:
            per_video[video.id] = rows
    best = sorted((rows[0] for rows in per_video.values()), key=lambda i: -i["score"])
    rest = sorted((i for rows in per_video.values() for i in rows[1:]), key=lambda i: -i["score"])
    return best+rest


MUSIC_TOPIC_MARKERS = ("music", "musik", "song", "genre", "band", "artist")


def music_profile(session, video, generic=None):
    """Das musikalische Profil eines eigenen Videos: Genres, Stimmungen, Orte, belegte Themen."""
    rows = profile_terms(session, video, generic)
    profile = {"genres": set(), "moods": set(), "places": set(), "topics": set(), "terms": {}, "rare": set()}
    for row in rows:
        profile["terms"][row["term"]] = row
        # Ein langer Begriff, der in keinem der gespeicherten fremden Titel vorkommt, ist wirklich
        # spezifisch: „mongolian“ ist etwas anderes als „long“.
        if row["df"] == 0 and len(row["term"]) >= 6:
            profile["rare"].add(row["term"])
        if row["category"] == "genre" and row["term"] not in MUSIC_WORDS:
            profile["genres"].add(row["term"])
        elif row["category"] == "mood":
            profile["moods"].add(row["term"])
        elif row["category"] == "ort":
            profile["places"].add(row["term"])
        elif row["category"] == "thema" and row["attestations"] >= MIN_ATTESTATIONS:
            profile["topics"].add(row["term"])
    return profile


def is_musical(words, topics=(), tags=()):
    """Ist der Kandidat selbst musikalisch klassifiziert – nach YouTube-Themen, Tags oder Genrewörtern?"""
    haystack = " ".join([" ".join(str(t) for t in topics or []), " ".join(str(t) for t in tags or [])]).lower()
    if any(marker in haystack for marker in MUSIC_TOPIC_MARKERS):
        return True
    return bool((set(words) & GENRE_WORDS) or (set(words) & MUSIC_WORDS))


def audience_fit(words, profile, intent_hit=None, neighbourhood=(), topics=(), tags=()):
    """Belegt dieser Ort eine echte Audience-Naehe – oder teilt er nur englische Woerter mit uns?

    Drei Klassen, absteigend nach Beweiskraft:

    neighbourhood  YouTube liefert uns dort schon aus, oder der Ort enthaelt genau diese Nachbarschaft.
    genre          Gemeinsames Genre oder gemeinsame Stimmung, und der Ort ist selbst musikalisch.
    topic          Mindestens zwei inhaltliche Begriffe, davon einer mit erkennbarer Kategorie
                   (Ort/Motiv oder ein mehrfach belegtes Thema) – „mongolian railway“, nicht „long out ride“.

    Alles andere ist kein Fit. Fuellwoerter, Credits und Formatwoerter zaehlen nie mit, egal wie viele.
    """
    words = {w for w in words if w not in HELPER_WORDS and w not in LABEL_WORDS}
    genres = sorted((profile["genres"] | profile["moods"]) & words)
    places = sorted(profile["places"] & words)
    topics_hit = sorted(profile["topics"] & words)
    semantic = places+topics_hit
    intent_head = sorted(set((intent_hit or {}).get("head") or []) & words)
    shared = sorted(set(genres+semantic+intent_head))
    if neighbourhood:
        return {"class": "neighbourhood", "genre": genres, "topic": semantic or intent_head,
                "shared": shared, "neighbourhood": list(neighbourhood)[:3],
                "why": ("YouTube liefert uns in dieser Nachbarschaft bereits aus: "
                        f"{', '.join(list(neighbourhood)[:2])}. Das ist gemessene Naehe, keine Wortaehnlichkeit.")}
    if genres and (is_musical(words, topics, tags) or len(shared) >= 2):
        return {"class": "genre", "genre": genres, "topic": semantic or intent_head, "shared": shared,
                "why": (f"Gemeinsames Genre bzw. gemeinsame Stimmung: {', '.join(genres)}"
                        + (f"; zusaetzlich inhaltlich: {', '.join(semantic or intent_head)}"
                           if (semantic or intent_head) else "")
                        + ". Der Ort ist selbst musikalisch klassifiziert."
                        if is_musical(words, topics, tags) else
                        f"Gemeinsames Genre bzw. gemeinsame Stimmung: {', '.join(genres)} und "
                        f"{len(shared)} inhaltliche Begriffe: {', '.join(shared)}.")}
    strong = sorted(set(semantic) | set(intent_head))
    if len(strong) == 1 and strong[0] in profile.get("rare", ()):
        # Ein einzelner, sehr spezifischer eigener Begriff traegt, wenn der Kandidat selbst derselben
        # Kategorie zugeordnet ist: „mongolian“ plus ein Reise-/Bahnwort ist ein Motiv, kein Wortzufall.
        family = (TRAVEL_WORDS if strong[0] in TRAVEL_WORDS else
                  GENRE_WORDS if strong[0] in GENRE_WORDS else
                  MOOD_WORDS if strong[0] in MOOD_WORDS else None)
        echo = sorted((words & family)-set(strong)) if family else []
        if echo:
            return {"class": "topic", "genre": genres, "topic": strong+echo, "shared": sorted(set(shared+echo)),
                    "why": (f"Der spezifische eigene Begriff „{strong[0]}“ steht dort, und der Ort ist derselben "
                            f"Kategorie zugeordnet ({', '.join(echo[:2])}). Das ist ein gemeinsames Motiv, keine "
                            "Wortgleichheit. Musikalische Naehe ist damit nicht belegt.")}
    if len(strong) >= MIN_ATTESTATIONS:
        return {"class": "topic", "genre": genres, "topic": strong, "shared": shared,
                "why": (f"Inhaltliche Naehe ueber {', '.join(strong)} – Begriffe mit erkennbarer Bedeutung "
                        "(Ort, Motiv oder mehrfach belegtes Thema), nicht bloss gemeinsame englische Woerter. "
                        "Musikalische Naehe ist damit nicht belegt: das Publikum teilt das Thema, nicht den Stil.")}
    return {"class": None, "genre": genres, "topic": strong, "shared": shared,
            "why": ("Keine belegbare Audience-Naehe: es bleiben "
                    + (f"nur {', '.join(shared)}" if shared else "keine inhaltlichen Begriffe")
                    + ". Gemeinsame Allerwelts- oder Fuellwoerter beweisen keine gemeinsame Zielgruppe.")}


def matches(intent, words):
    """Passt ein gefundener Ort zu diesem Intent – und zwar belegbar, nicht nach Bauchgefühl?

    Der Intent selbst ist aus unseren Daten belegt. Enthält der Kandidat den Themenkopf, ist der Bezug
    hergestellt: wir haben genau danach gesucht, weil es unser Thema ist. Ein einfach belegter Intent
    muss zusätzlich seinen Kontext treffen, damit ein Zufallstreffer nicht als Thema durchgeht.
    """
    head_hits = [t for t in intent["head"] if t in words]
    context_hits = [t for t in intent.get("context") or [] if t in words]
    if not head_hits:
        return None
    if len(head_hits) < len(intent["head"]) and (intent.get("attestations") or 0) < MIN_ATTESTATIONS             and not context_hits:
        # Einfach belegter Intent, nur teilweise getroffen: das kann Zufall sein, also nicht ohne Kontext.
        return None
    return {"head": head_hits, "context": context_hits, "label": intent.get("label"),
            "kind": intent.get("kind"), "query": intent.get("query"),
            "attestations": intent.get("attestations")}
