import argparse
import logging
import time
from .youtube import authorize
from .pipeline import collect
from .config import settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["oauth", "sync", "worker", "demo"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "oauth":
        authorize()
    elif args.command == "demo":
        from .demo import seed
        seed()
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
