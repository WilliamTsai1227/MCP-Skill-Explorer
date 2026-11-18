"""Run one crawl + analysis without the web server (for cron/launchd).

    python -m backend.run_once
"""
from . import db
from .app import run_job

if __name__ == "__main__":
    db.init()
    run_job()
