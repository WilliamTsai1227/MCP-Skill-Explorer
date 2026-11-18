"""Crawl GitHub for SKILL.md files from configured sources.

Rate-limit friendly:
  * 1 metadata + 1 git-tree API call per repo, with ETag caching (304s are free).
  * File contents come from raw.githubusercontent.com (not counted against the API quota).
Set GITHUB_TOKEN to raise the quota from 60 to 5000 requests/hour.
"""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
import re
from datetime import datetime, timezone
from pathlib import Path
import json

import httpx
import yaml

from . import db

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


def load_config():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RateLimited(Exception):
    pass


class Crawler:
    def __init__(self, log):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "skill-explorer"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(headers=headers, timeout=30, follow_redirects=True)
        self.log = log

    # ---------- GitHub API helpers ----------
    def _api(self, url, etag=None, params=None):
        h = {"If-None-Match": etag} if etag else {}
        r = self.http.get(url, headers=h, params=params)
        if r.status_code == 304:
            return None, etag, True
        if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimited(f"GitHub API rate limit hit (reset at {r.headers.get('x-ratelimit-reset')})")
        r.raise_for_status()
        return r.json(), r.headers.get("etag"), False

    def repo_info(self, full_name):
        """Return (meta_row, tree_paths). Uses cached copy on 304."""
        with db.conn() as c:
            cached = c.execute("SELECT * FROM repos WHERE full_name=?", (full_name,)).fetchone()
        cached = dict(cached) if cached else {}

        meta, etag_meta, hit = self._api(f"{API}/repos/{full_name}", cached.get("etag_meta"))
        if hit:
            stars, desc, branch = cached["stars"], cached["description"], cached["default_branch"]
        else:
            full_name = meta["full_name"]  # follow renames
            stars, desc, branch = meta["stargazers_count"], meta.get("description"), meta["default_branch"]

        tree_json, etag_tree, hit = self._api(
            f"{API}/repos/{full_name}/git/trees/{branch}", cached.get("etag_tree"), params={"recursive": "1"})
        tree_unchanged = hit
        if hit:
            paths = json.loads(cached["tree"])
        else:
            paths = [t["path"] for t in tree_json.get("tree", []) if t.get("type") == "blob"]
            if tree_json.get("truncated"):
                self.log(f"  ! tree truncated for {full_name}")

        with db.conn() as c:
            c.execute(
                """INSERT INTO repos VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(full_name) DO UPDATE SET stars=excluded.stars, description=excluded.description,
                   default_branch=excluded.default_branch, etag_meta=excluded.etag_meta,
                   etag_tree=excluded.etag_tree, tree=excluded.tree, updated=excluded.updated""",
                (full_name, stars, desc, branch, etag_meta, etag_tree, json.dumps(paths), now()))
        return {"full_name": full_name, "stars": stars, "description": desc, "branch": branch,
                "tree_unchanged": tree_unchanged}, paths

    def search_repos(self, query, top):
        data, _, _ = self._api(f"{API}/search/repositories",
                               params={"q": query, "sort": "stars", "order": "desc", "per_page": top})
        return [i["full_name"] for i in data.get("items", [])]

    def raw(self, full_name, branch, path):
        r = self.http.get(f"{RAW}/{full_name}/{branch}/{path}")
        r.raise_for_status()
        return r.text

    # ---------- skill extraction ----------
    def scan_repo(self, full_name, src, max_skills=None):
        meta, paths = self.repo_info(full_name)
        full_name = meta["full_name"]
        if meta["tree_unchanged"]:
            # repo has not changed since last crawl -> reuse stored files, no downloads at all
            with db.conn() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM skills WHERE repo=?", (full_name,))]
            if rows:
                for r in rows:
                    r.update(vendor=src["vendor"], source_id=src["id"],
                             official=1 if src.get("official") else 0, stars=meta["stars"])
                self.log(f"  {full_name} ★{meta['stars']}: unchanged ({len(rows)} skills, cached)")
                return rows
        skill_files = sorted(p for p in paths if p.rsplit("/", 1)[-1].lower() == "skill.md")
        # skip templates / tests
        skill_files = [p for p in skill_files if not re.search(r"(^|/)(template|templates|test|tests|node_modules)/", p, re.I)]
        if max_skills:
            skill_files = skill_files[:max_skills]
        self.log(f"  {full_name} ★{meta['stars']}: {len(skill_files)} SKILL.md")

        def get(p):
            try:
                return p, self.raw(full_name, meta["branch"], p)
            except httpx.HTTPError as e:
                self.log(f"  ! {p}: {e}")
                return p, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            texts = list(pool.map(get, skill_files))

        found = []
        for p, text in texts:
            if text is None:
                continue
            fm, _body = parse_frontmatter(text)
            folder = p.rsplit("/", 1)[0] if "/" in p else ""
            siblings = [x for x in paths if folder and x.startswith(folder + "/") and x != p]
            name = str(fm.get("name") or (folder.rsplit("/", 1)[-1] if folder else full_name.split("/")[1]))
            found.append({
                "id": f"{full_name}:{p}",
                "name": name,
                "description": str(fm.get("description") or "").strip(),
                "repo": full_name,
                "path": p,
                "url": f"https://github.com/{full_name}/tree/{meta['branch']}/{folder}" if folder
                       else f"https://github.com/{full_name}",
                "vendor": src["vendor"],
                "source_id": src["id"],
                "official": 1 if src.get("official") else 0,
                "stars": meta["stars"],
                "content": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "n_scripts": sum(1 for x in siblings if re.search(r"/(scripts?|bin)/", "/" + x[len(folder):])
                                 or x.endswith((".py", ".sh", ".js", ".ts"))),
                "n_refs": sum(1 for x in siblings if x.endswith(".md")),
                "n_files": len(siblings) + 1,
            })
        return found


def parse_frontmatter(text):
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip("\"'")
    return fm, m.group(2)


def upsert(skills):
    ts = now()
    new = 0
    with db.conn() as c:
        for s in skills:
            exists = c.execute("SELECT 1 FROM skills WHERE id=?", (s["id"],)).fetchone()
            if exists:
                c.execute("""UPDATE skills SET name=?, description=?, url=?, vendor=?, source_id=?, official=?,
                             stars=?, content=?, content_hash=?, n_scripts=?, n_refs=?, n_files=?, last_seen=?
                             WHERE id=?""",
                          (s["name"], s["description"], s["url"], s["vendor"], s["source_id"], s["official"],
                           s["stars"], s["content"], s["content_hash"], s["n_scripts"], s["n_refs"],
                           s["n_files"], ts, s["id"]))
            else:
                new += 1
                c.execute("""INSERT INTO skills (id,name,description,repo,path,url,vendor,source_id,official,stars,
                             content,content_hash,n_scripts,n_refs,n_files,first_seen,last_seen)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (s["id"], s["name"], s["description"], s["repo"], s["path"], s["url"], s["vendor"],
                           s["source_id"], s["official"], s["stars"], s["content"], s["content_hash"],
                           s["n_scripts"], s["n_refs"], s["n_files"], ts, ts))
    return new


def crawl(log):
    """Crawl all sources. Returns (total_seen, new_count)."""
    cfg = load_config()
    cr = Crawler(log)
    seen_repos, total, new = set(), 0, 0
    try:
        for src in cfg.get("sources", []):
            log(f"[{src['id']}] {src['type']}")
            try:
                if src["type"] == "repo":
                    repos = [src["repo"]]
                elif src["type"] == "search":
                    repos = cr.search_repos(src["query"], src.get("top", 10))
                else:
                    log(f"  ! unknown type {src['type']}")
                    continue
                for full in repos:
                    if full.lower() in seen_repos:   # official sources win over search dupes
                        continue
                    seen_repos.add(full.lower())
                    try:
                        skills = cr.scan_repo(full, src, src.get("max_skills_per_repo"))
                    except httpx.HTTPStatusError as e:
                        log(f"  ! {full}: HTTP {e.response.status_code}")
                        continue
                    total += len(skills)
                    new += upsert(skills)
            except RateLimited:
                raise
            except Exception as e:  # keep going with other sources
                log(f"  ! source failed: {e!r}")
    except RateLimited as e:
        log(f"STOP: {e}. 設定 GITHUB_TOKEN 可提高額度。")
    finally:
        cr.http.close()
    dedupe(log)
    return total, new


def dedupe(log):
    """Hide copies: same SKILL.md content anywhere, or same skill name inside one repo
    (big repos ship the same skill in several plugin folders). Keeps official / most-starred /
    shortest path. Hidden rows stay in the DB but are not shown, summarised or emailed."""
    with db.conn() as c:
        rows = c.execute("SELECT id, repo, name, content_hash, official, stars, path FROM skills "
                         "WHERE kind = 'skill'").fetchall()
        rows = sorted(rows, key=lambda r: (-r["official"], -r["stars"], len(r["path"]), r["id"]))
        seen_hash, seen_name, hidden = set(), set(), []
        for r in rows:
            key = (r["repo"].lower(), r["name"].lower())
            hidden.append((1 if r["content_hash"] in seen_hash or key in seen_name else 0, r["id"]))
            seen_hash.add(r["content_hash"])
            seen_name.add(key)
        c.executemany("UPDATE skills SET hidden=? WHERE id=?", hidden)
    n = sum(h for h, _ in hidden)
    log(f"dedupe: {len(rows) - n} unique, {n} duplicates hidden")
