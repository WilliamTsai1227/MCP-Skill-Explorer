"""Skill Explorer API + scheduler + static frontend.

Run:  python -m uvicorn backend.app:app --port 8000   (from the Skill_explorer folder)
"""
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import analyzer, db, digest, fetcher, mcp, ranking

ROOT = Path(__file__).resolve().parent.parent
_run_lock = threading.Lock()
scheduler = BackgroundScheduler()


def run_job():
    """One full crawl + analysis pass. Safe to call from scheduler or API."""
    if not _run_lock.acquire(blocking=False):
        return False
    lines = []
    started = fetcher.now()
    with db.conn() as c:
        run_id = c.execute("INSERT INTO runs (started, status) VALUES (?, 'running')",
                           (started,)).lastrowid

    def log(msg):
        lines.append(f"{datetime.now():%H:%M:%S} {msg}")
        print(msg, flush=True)
        with db.conn() as c:
            c.execute("UPDATE runs SET log=? WHERE id=?", ("\n".join(lines), run_id))

    status, summary = "ok", ""
    try:
        cfg = fetcher.load_config()
        total, new = fetcher.crawl(log)
        summary = f"掃描 {total} 個 skill，新增 {new} 個"
        mcfg = cfg.get("mcp") or {}
        if mcfg.get("enabled", True):
            log("[mcp-registry]")
            try:
                kept, mnew = mcp.sync(log, mcfg)
                summary += f"；MCP {kept} 個，新增 {mnew} 個"
            except Exception as e:  # registry problems must not block skills
                log(f"  ! MCP sync failed: {e!r}")
        analyzer.summarize_pending(log, cfg.get("analysis", {}))
        analyzer.analyze_pending(log, cfg.get("analysis", {}))
        log(f"backup: {db.backup().name}")
        try:
            digest.save_and_send(started, log)
        except Exception as e:  # mail problems must not fail the run
            log(f"  ! digest failed: {e!r}")
    except Exception as e:
        status, summary = "error", repr(e)
        log(f"ERROR {e!r}")
    finally:
        with db.conn() as c:
            c.execute("UPDATE runs SET finished=?, status=?, summary=?, log=? WHERE id=?",
                      (fetcher.now(), status, summary, "\n".join(lines), run_id))
        _run_lock.release()
    return True


def start_bg():
    if _run_lock.locked():
        return False
    threading.Thread(target=run_job, daemon=True).start()
    return True


app = FastAPI(title="Skill Explorer")


@app.on_event("startup")
def startup():
    db.init()
    with db.conn() as c:  # mark runs interrupted by a restart
        c.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
    cfg = fetcher.load_config().get("schedule", {})
    scheduler.add_job(run_job, CronTrigger.from_crontab(cfg.get("cron", "0 3 * * *")), id="crawl",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.start()
    if cfg.get("run_on_startup_if_empty", True):
        with db.conn() as c:
            if c.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0:
                start_bg()


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


@app.get("/api/skills")
def list_skills(kind: str = "skill"):
    """All skills (kind=skill) or MCP servers (kind=mcp), ranked 'for you' (see ranking.py)."""
    with db.conn() as c:
        rows = c.execute("SELECT * FROM skills WHERE hidden = 0 AND kind = ?", (kind,)).fetchall()
    cfg = fetcher.load_config()
    ranked = ranking.rank([db.row_to_skill(r) for r in rows], cfg.get("interests"))
    picked = {s["id"] for s in ranking.curate_for(ranked, kind, cfg)}
    for s in ranked:
        s["curated"] = s["id"] in picked
    return ranked


class Feedback(BaseModel):
    id: str
    value: int  # 1 like, -1 dislike, 0 clear


@app.post("/api/feedback")
def feedback(fb: Feedback):
    if fb.value not in (-1, 0, 1):
        raise HTTPException(400, "value must be -1, 0 or 1")
    with db.conn() as c:
        n = c.execute("UPDATE skills SET feedback=? WHERE id=?", (fb.value, fb.id)).rowcount
    if not n:
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/digest/send")
def digest_send(hours: int = 24):
    """Build (and email, if SMTP is configured) a digest covering the last `hours`."""
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    logs = []
    try:
        sent = digest.save_and_send(since, logs.append)
    except Exception as e:
        raise HTTPException(500, f"寄信失敗：{e!r}")
    return {"sent": sent, "log": logs}


@app.get("/api/digest/latest")
def digest_latest(all: bool = False):
    files = sorted((db.DB_PATH.parent / "digests").glob("*-all.html" if all else "????-??-??.html"))
    if not files:
        raise HTTPException(404, "還沒有日報")
    return FileResponse(files[-1], media_type="text/html")


@app.get("/api/skill")
def get_skill(id: str):
    with db.conn() as c:
        r = c.execute("SELECT * FROM skills WHERE id=?", (id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return db.row_to_skill(r, with_content=True)


@app.get("/api/status")
def status():
    with db.conn() as c:
        last = c.execute("SELECT id, started, finished, status, summary FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        total = c.execute("SELECT COUNT(*) FROM skills WHERE hidden = 0 AND kind='skill'").fetchone()[0]
        total_mcp = c.execute("SELECT COUNT(*) FROM skills WHERE hidden = 0 AND kind='mcp'").fetchone()[0]
        dups = c.execute("SELECT COUNT(*) FROM skills WHERE hidden = 1 AND kind='skill'").fetchone()[0]
        llm = c.execute("SELECT COUNT(*) FROM skills WHERE hidden = 0 AND analysis_by IS NOT NULL AND analysis_by!='rules'").fetchone()[0]
        summarized = c.execute("SELECT COUNT(*) FROM skills WHERE hidden = 0 AND summary_hash LIKE ?",
                               (analyzer.SUMMARY_VERSION + ":%",)).fetchone()[0]
    job = scheduler.get_job("crawl")
    return {
        "running": _run_lock.locked(),
        "last_run": dict(last) if last else None,
        "next_run": job.next_run_time.astimezone(timezone.utc).isoformat() if job and job.next_run_time else None,
        "total": total,
        "total_mcp": total_mcp,
        "llm_analyzed": llm,
        "summarized": summarized,
        "duplicates": dups,
        "email": bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_TO")
                      and (os.environ.get("SMTP_PASSWORD") or not os.environ.get("SMTP_USER"))),
        "sources": fetcher.load_config().get("sources", []),
    }


@app.get("/api/runs")
def runs(limit: int = 10):
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


@app.post("/api/refresh")
def refresh():
    return {"started": start_bg()}


app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend" / "index.html")
