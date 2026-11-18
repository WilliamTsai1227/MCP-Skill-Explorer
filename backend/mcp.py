"""Official MCP Registry (registry.modelcontextprotocol.io) -> filtered, de-duplicated MCP servers.

The registry has ~67k servers, many of them spam (single publishers with thousands of entries),
and no popularity signal. We keep a server only if
  * it is active (not deprecated), latest version, and its publisher namespace is not a bulk spammer, and
  * its GitHub repo has >= min_stars, or its namespace is in `trusted_namespaces` (big vendors' own domains).
Only one server per GitHub repo is kept. Stars come from the GitHub GraphQL API (100 repos per query)
and are cached in the `gh_stars` table for `stars_ttl_days`.
"""
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .fetcher import now

REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"
META = "io.modelcontextprotocol.registry/official"
GH_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def _repo(server):
    m = GH_RE.match((server.get("repository") or {}).get("url") or "")
    return f"{m[1]}/{m[2].removesuffix('.git')}" if m else None


def fetch_registry(log):
    http = httpx.Client(timeout=60, headers={"User-Agent": "skill-explorer"})
    out, cursor = [], None
    try:
        while True:
            params = {"limit": 100, "version": "latest"}
            if cursor:
                params["cursor"] = cursor
            for attempt in range(3):
                try:
                    r = http.get(REGISTRY, params=params)
                    r.raise_for_status()
                    break
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                    time.sleep(3)
            d = r.json()
            out += d.get("servers", [])
            cursor = (d.get("metadata") or {}).get("nextCursor")
            if not cursor or not d.get("servers"):
                break
    finally:
        http.close()
    log(f"  registry: {len(out)} servers (latest versions)")
    return out


def lookup_stars(repos, log, ttl_days):
    """Return {owner/name(lower): stars}, using/refreshing the gh_stars cache."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    fresh_after = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    with db.conn() as c:
        cache = {r["repo"]: (r["stars"], r["checked"]) for r in c.execute("SELECT * FROM gh_stars")}
    todo = sorted({r for r in repos if r.lower() not in cache or cache[r.lower()][1] < fresh_after})
    if todo and not token:
        log(f"  ! {len(todo)} repos need star counts but GITHUB_TOKEN is not set; only trusted namespaces kept")
        todo = []
    if todo:
        log(f"  stars: looking up {len(todo)} repos via GraphQL")
    http = httpx.Client(timeout=90, headers={"Authorization": f"Bearer {token}"}) if todo else None
    for k in range(0, len(todo), 100):
        chunk = todo[k:k + 100]
        q = "query{" + " ".join(
            f"r{i}:repository(owner:{json.dumps(r.split('/')[0])},name:{json.dumps(r.split('/')[1])}){{stargazerCount}}"
            for i, r in enumerate(chunk)) + "}"
        data = None
        for attempt in range(3):
            try:
                resp = http.post("https://api.github.com/graphql", json={"query": q})
                if resp.status_code == 200:
                    data = resp.json().get("data") or {}
                    break
            except httpx.HTTPError:
                pass
            time.sleep(5 * (attempt + 1))
        if data is None:
            log(f"  ! star batch {k // 100} failed")
            continue
        ts = now()
        with db.conn() as c:
            c.executemany(
                "INSERT INTO gh_stars VALUES (?,?,?) ON CONFLICT(repo) DO UPDATE SET stars=excluded.stars, checked=excluded.checked",
                [(r.lower(), (data.get(f"r{i}") or {}).get("stargazerCount", -1), ts) for i, r in enumerate(chunk)])
    if http:
        http.close()
    with db.conn() as c:
        return {r["repo"]: r["stars"] for r in c.execute("SELECT repo, stars FROM gh_stars")}


def sync(log, cfg):
    """Fetch, filter and upsert MCP servers. Returns (kept, new)."""
    min_stars = cfg.get("min_stars", 300)
    spam_cap = cfg.get("max_servers_per_namespace", 20)
    trusted = {n.lower() for n in cfg.get("trusted_namespaces", [])}

    servers = fetch_registry(log)
    active = [s for s in servers if (s.get("_meta", {}).get(META) or {}).get("status", "active") == "active"]
    per_ns = Counter(s["server"]["name"].split("/")[0].lower() for s in active)
    cands = [s for s in active if per_ns[s["server"]["name"].split("/")[0].lower()] <= spam_cap
             or s["server"]["name"].split("/")[0].lower() in trusted]
    stars = lookup_stars({_repo(s["server"]) for s in cands if _repo(s["server"])}, log, cfg.get("stars_ttl_days", 7))

    best = {}  # one server per repo (or per name when there is no repo)
    for s in cands:
        sv = s["server"]
        ns = sv["name"].split("/")[0].lower()
        repo = _repo(sv)
        st = stars.get(repo.lower(), -1) if repo else -1
        if st < min_stars and ns not in trusted:
            continue
        key = (repo or sv["name"]).lower()
        prev = best.get(key)
        if not prev or len(sv["name"]) < len(prev[0]["name"]):
            best[key] = (sv, s.get("_meta", {}).get(META) or {}, max(st, 0), repo, ns)
    log(f"  kept {len(best)} MCP servers (stars >= {min_stars} or trusted namespace)")

    ts, new, seen = now(), 0, []
    with db.conn() as c:
        for sv, meta, st, repo, ns in best.values():
            sid = f"mcp:{sv['name']}"
            seen.append(sid)
            install = {k: sv.get(k) for k in ("name", "version", "remotes", "packages", "websiteUrl", "repository") if sv.get(k)}
            content = json.dumps(install, ensure_ascii=False, indent=2)
            desc = (sv.get("description") or "").strip()
            row = dict(
                name=sv.get("title") or sv["name"].split("/")[-1], description=desc,
                repo=repo or sv["name"], path=sv["name"],
                url=f"https://github.com/{repo}" if repo else (sv.get("websiteUrl") or f"{REGISTRY}?search={sv['name']}"),
                vendor=(repo.split("/")[0] if repo else ns), source_id="mcp-registry",
                official=0 if ns.startswith("io.github.") else 1,  # domain-verified publisher = the company itself
                stars=st, content=content,
                content_hash=hashlib.sha256(f"{sv['name']}\n{desc}\n{repo}".encode()).hexdigest(),
                n_scripts=0, n_refs=0, n_files=len(sv.get("packages") or []) + len(sv.get("remotes") or []),
            )
            if c.execute("SELECT 1 FROM skills WHERE id=?", (sid,)).fetchone():
                c.execute("""UPDATE skills SET name=:name, description=:description, repo=:repo, path=:path, url=:url,
                             vendor=:vendor, official=:official, stars=:stars, content=:content,
                             content_hash=:content_hash, n_files=:n_files, last_seen=:ts, hidden=0 WHERE id=:id""",
                          {**row, "ts": ts, "id": sid})
            else:
                new += 1
                c.execute("""INSERT INTO skills (id, kind, name, description, repo, path, url, vendor, source_id, official,
                             stars, content, content_hash, n_scripts, n_refs, n_files, first_seen, last_seen, hidden)
                             VALUES (:id, 'mcp', :name, :description, :repo, :path, :url, :vendor, :source_id, :official,
                             :stars, :content, :content_hash, :n_scripts, :n_refs, :n_files, :ts, :ts, 0)""",
                          {**row, "ts": ts, "id": sid})
        # servers that dropped out of the filter (deprecated, lost stars, ...) are hidden, not deleted
        c.execute(f"UPDATE skills SET hidden=1 WHERE kind='mcp' AND id NOT IN ({','.join('?' * len(seen)) or 'NULL'})", seen)
    return len(best), new
