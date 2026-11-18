"""SQLite storage for skills, repo cache (ETags) and fetch runs."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "skills.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,            -- owner/repo:path/to/SKILL.md
    name TEXT, description TEXT,
    repo TEXT, path TEXT, url TEXT,
    vendor TEXT, source_id TEXT, official INTEGER DEFAULT 0,
    stars INTEGER DEFAULT 0,
    content TEXT, content_hash TEXT,
    n_scripts INTEGER DEFAULT 0, n_refs INTEGER DEFAULT 0, n_files INTEGER DEFAULT 0,
    first_seen TEXT, last_seen TEXT,
    analysis TEXT, analysis_hash TEXT, analysis_by TEXT
);
CREATE TABLE IF NOT EXISTS repos (
    full_name TEXT PRIMARY KEY,
    stars INTEGER, description TEXT, default_branch TEXT,
    etag_meta TEXT, etag_tree TEXT, tree TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS gh_stars (
    repo TEXT PRIMARY KEY, stars INTEGER, checked TEXT   -- star cache for MCP server repos
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT, finished TEXT, status TEXT, summary TEXT, log TEXT
);
"""


@contextmanager
def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


MIGRATIONS = {  # column -> DDL; applied once if missing (keeps existing data)
    "summary_zh": "ALTER TABLE skills ADD COLUMN summary_zh TEXT",        # short Chinese "what it does"
    "summary_hash": "ALTER TABLE skills ADD COLUMN summary_hash TEXT",
    "feedback": "ALTER TABLE skills ADD COLUMN feedback INTEGER DEFAULT 0",  # 1 like, -1 dislike
    "hidden": "ALTER TABLE skills ADD COLUMN hidden INTEGER DEFAULT 0",      # 1 = duplicate / filtered out
    "kind": "ALTER TABLE skills ADD COLUMN kind TEXT DEFAULT 'skill'",       # 'skill' or 'mcp'
}


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(skills)")}
        for col, ddl in MIGRATIONS.items():
            if col not in cols:
                c.execute(ddl)


def backup(keep=14):
    """Consistent online copy to data/backups/skills-YYYYmmdd.db; keeps the newest `keep` files."""
    from datetime import datetime
    bdir = DB_PATH.parent / "backups"
    bdir.mkdir(exist_ok=True)
    dest = bdir / f"skills-{datetime.now():%Y%m%d}.db"
    with _lock:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for old in sorted(bdir.glob("skills-*.db"))[:-keep]:
        old.unlink()
    return dest


def row_to_skill(r, with_content=False):
    d = dict(r)
    d["analysis"] = json.loads(d["analysis"]) if d.get("analysis") else None
    d.pop("summary_hash", None)
    d.pop("analysis_hash", None)
    d.pop("content_hash", None)
    if not with_content:
        d.pop("content", None)
    return d
