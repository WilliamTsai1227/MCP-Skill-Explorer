"""'For you' ranking.

score = popularity (stars, official)
      + interest keywords from config (`interests:`)
      + similarity to skills you liked (category / tags / repo / words)
      - similarity to skills you disliked
      + your own 👍 (pinned near the top) / 👎 (sunk to the bottom)
      + small boost for skills discovered in the last 3 days
"""
import math
import re
from datetime import datetime, timedelta, timezone

WORD = re.compile(r"[a-z][a-z0-9]{2,}")
STOP = {"the", "and", "for", "with", "use", "when", "this", "that", "from", "your", "skill", "skills",
        "using", "user", "users", "should", "into", "any", "are", "you", "can", "also", "will"}


def _features(s):
    a = s.get("analysis") or {}
    text = f"{s['name']} {s.get('description') or ''} {' '.join(a.get('tags') or [])}".lower()
    words = {w for w in WORD.findall(text.replace("-", " ")) if w not in STOP}
    return {
        "cat": a.get("category"),
        "tags": set(t.lower() for t in (a.get("tags") or [])),
        "repo": s["repo"],
        "words": words,
        "blob": f"{text} {s.get('summary_zh') or ''} {a.get('one_liner') or ''}".lower(),
    }


def _sim(f, g):
    jac = len(f["words"] & g["words"]) / (len(f["words"] | g["words"]) or 1)
    return (1.0 if f["cat"] and f["cat"] == g["cat"] else 0) \
        + 0.5 * len(f["tags"] & g["tags"]) \
        + (0.5 if f["repo"] == g["repo"] else 0) \
        + 4 * jac


def rank(skills, interests=()):
    """Adds `score` and `reasons` to each skill dict, returns them sorted best-first."""
    feats = {s["id"]: _features(s) for s in skills}
    liked = [feats[s["id"]] for s in skills if s.get("feedback") == 1]
    disliked = [feats[s["id"]] for s in skills if s.get("feedback") == -1]
    interests = [i.lower() for i in interests or []]
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    for s in skills:
        f = feats[s["id"]]
        reasons = []
        score = 0.6 * math.log10((s.get("stars") or 0) + 1) + (1.0 if s.get("official") else 0)
        hits = [k for k in interests if k in f["blob"]]
        if hits:
            score += 0.8 * min(len(hits), 2)
            reasons.append("符合興趣：" + "、".join(hits[:3]))
        if liked:
            sim = sum(_sim(f, g) for g in liked) / len(liked)
            score += 2.5 * sim
            if sim > 1.2:
                reasons.append("和你按讚的 skill 相似")
        if disliked:
            score -= 1.5 * sum(_sim(f, g) for g in disliked) / len(disliked)
        if s.get("feedback") == 1:
            score += 8
            reasons.insert(0, "你按過讚")
        elif s.get("feedback") == -1:
            score -= 50
        if (s.get("first_seen") or "") >= recent:
            score += 0.5
        s["score"] = round(score, 3)
        s["reasons"] = reasons
    return sorted(skills, key=lambda s: -s["score"])


def curate(ranked, n=150, per_repo=12, per_official_min=3, group="repo"):
    """Top-n of an already ranked list.
    * at most `per_repo` from one repo, so huge vendor repos don't crowd everything out
    * every official repo gets its best `per_official_min` skills in, even with few stars
    * liked skills always make it in, disliked never do"""
    pool = [s for s in ranked if s.get("feedback") != -1]
    out, ids, per = [], set(), {}

    def take(s):
        out.append(s)
        ids.add(s["id"])
        per[s[group]] = per.get(s[group], 0) + 1

    for s in pool:  # liked + per-vendor guarantees first
        if s.get("feedback") == 1 or (s.get("official") and per.get(s[group], 0) < per_official_min):
            take(s)
    for s in pool:
        if len(out) >= n:
            break
        if s["id"] not in ids and per.get(s[group], 0) < per_repo:
            take(s)
    return sorted(out, key=lambda s: -s["score"])


def curate_for(ranked, kind, cfg):
    """Curated picks for 'skill' (config `curated`) or 'mcp' (config `mcp.curated`)."""
    if kind == "mcp":
        cur = (cfg.get("mcp") or {}).get("curated", {})
        # MCP is already one server per repo -> cap per publisher instead; no official guarantees
        return curate(ranked, cur.get("size", 100), cur.get("per_owner", 3), per_official_min=0, group="vendor")
    cur = cfg.get("curated", {})
    return curate(ranked, cur.get("size", 150), cur.get("per_repo", 12))
