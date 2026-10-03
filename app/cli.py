import argparse
import logging
import os
import time
from .youtube import authorize
from .pipeline import collect
from .config import settings


def _content(args):
    """Lokaler Lauf: eigene Dateien messen, Kandidaten bilden, optional schneiden.

    Nur Messwerte und Zeitmarken gehen in die Datenbank – die Mediendateien bleiben, wo sie sind.
    """
    from datetime import date
    from sqlalchemy import select
    from . import content as ci
    from . import media
    from .db import Session
    from .models import MediaAsset, Video
    media.local_only()
    today = date.today()
    with Session() as session:
        touched = []
        if args.masters:
            files = media.masters(args.masters)
            if not files:
                print(f"Keine Mediendateien in {args.masters}")
            for path in files:
                result = media.ingest(session, path, today, args.reanalyse)
                session.commit()
                print(f"{result['status']:>9}  {result['path']}")
                if result["status"] == "analysed":
                    print(f"           Dauer {result['duration']:.1f} s · Abschnitte {result['sections']} · "
                          f"Textzeilen {result['lines']} · Video: {result.get('title') or 'nicht zugeordnet'}")
                if result.get("note"):
                    print(f"           {result['note']}")
                if result["status"] != "error":
                    touched.append(result["asset_id"])
        query = select(MediaAsset).where(MediaAsset.status == "analysed")
        if args.video_id:
            query = query.where(MediaAsset.video_id == args.video_id)
        elif touched:
            query = query.where(MediaAsset.id.in_(touched))
        learned = ci.priors(session, today)
        for asset in session.scalars(query.order_by(MediaAsset.path)):
            video = session.get(Video, asset.video_id) if asset.video_id else None
            found = ci.candidates_for(session, asset, video, None, learned)
            if not found:
                print(f"\n{asset.path}: kein Kandidat aus dem gemessenen Material")
                continue
            stored = ci.store(session, video, found, today)
            session.commit()
            print(f"\n{video.title if video else os.path.basename(asset.path)}: {len(stored)} Kandidaten")
            print(f"  Lernstand: {learned['note']}")
            for candidate in stored:
                print(f"  #{candidate['rank']} Kandidat {candidate['candidate_id']}: "
                      f"{candidate['start_seconds']:.1f}-{candidate['end_seconds']:.1f} s "
                      f"({candidate['duration_seconds']:.1f} s)")
                if candidate.get("hook"):
                    print(f"     Hook ({candidate['hook_source']}): {candidate['hook']}")
                measured = " · ".join(f"{ci.LABELS.get(k, k)} {v:.2f}"
                                      for k, v in candidate["properties"].items() if v is not None)
                print(f"     Messwerte: {measured}")
                for line in candidate["evidence"]:
                    print(f"     - {line}")
                if args.render:
                    print(f"     gerendert: {media.render(session, candidate['candidate_id'], args.render)}")
                    session.commit()


def _published(args):
    """Den hochgeladenen Short mit seinem Kandidaten verbinden – die Grundlage fuer das Lernen."""
    from datetime import date
    from . import content as ci
    from .db import Session
    with Session() as session:
        row = ci.attach_published(session, args.candidate, args.video_id,
                                  date.fromisoformat(args.day) if args.day else date.today())
        if row is None:
            raise SystemExit(f"Kandidat {args.candidate} existiert nicht.")
        session.commit()
        print(f"Kandidat {row.id} ({row.start_seconds:.1f}–{row.end_seconds:.1f} s) ist jetzt Video "
              f"{row.published_video_id} vom {row.published_day}. Seine Impressions, Views, "
              "Trafficquellen und Retention kommen ueber den normalen Sync.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["oauth", "sync", "worker", "demo", "content", "published"])
    parser.add_argument("--masters", help="Verzeichnis mit eigenen Originaldateien (nur `content`)")
    parser.add_argument("--render", help="Zielverzeichnis fuer geschnittene Shorts (nur `content`)")
    parser.add_argument("--reanalyse", action="store_true",
                        help="Unveraenderte Dateien erneut messen (nach Aenderung der Messung)")
    parser.add_argument("--video-id", help="Auf dieses Video beschraenken bzw. das veroeffentlichte Video")
    parser.add_argument("--candidate", type=int, help="Kandidaten-ID (nur `published`)")
    parser.add_argument("--day", help="Veroeffentlichungstag JJJJ-MM-TT (nur `published`)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "oauth":
        authorize()
    elif args.command == "demo":
        from .demo import seed
        seed()
    elif args.command == "content":
        _content(args)
    elif args.command == "published":
        if not args.candidate or not args.video_id:
            raise SystemExit("--candidate und --video-id sind erforderlich.")
        _published(args)
    elif args.command == "sync":
        result = collect()
        print(result)
        if result["status"] not in ("ok", "already_running"):
            raise SystemExit(1)
    else:
        if settings.hosted:
            raise SystemExit("Use Vercel Cron; persistent workers are disabled in hosted environments.")
        while True:
            try:
                logging.info("Sync result: %s", collect())
            except Exception as exc:
                logging.error("Worker cycle failed: %s", type(exc).__name__)
            time.sleep(max(300, settings.sync_interval_seconds))


if __name__ == "__main__":
    main()
