"""Daily email digest: what's new today + every skill ranked 'for you'.

Gmail clips message bodies over ~102KB, so the body holds today's new skills and the
top recommendations; the complete ranked list of ALL skills is attached as an HTML file.
Every digest is also saved to data/digests/YYYY-mm-dd.html.
"""
import html
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

from . import db, fetcher, ranking

TOP_IN_BODY = 120   # skills in the email body
MCP_IN_BODY = 40    # MCP servers in the email body


def _e(x):
    return html.escape(str(x or ""))


def _row(s):
    zh = s.get("summary_zh") or (s.get("analysis") or {}).get("one_liner") or s.get("description") or ""
    badge = "官方 · " if s.get("official") else ""
    why = f' <span style="color:#2b63c4">（{_e("；".join(s["reasons"]))}）</span>' if s.get("reasons") else ""
    return (f'<li style="margin:0 0 6px"><a href="{_e(s["url"])}" style="color:#c4562b;font-weight:600;'
            f'text-decoration:none">{_e(s["name"])}</a> <span style="color:#888;font-size:12px">'
            f'{badge}{_e(s["vendor"])} ★{s["stars"]:,}</span><br>{_e(zh[:160])}{why}</li>')


def _page(title, sections):
    body = "".join(
        f'<h2 style="font-size:17px;margin:24px 0 8px">{_e(h)}</h2>{note}'
        f'<ol style="padding-left:22px;margin:0">{"".join(_row(s) for s in items)}</ol>'
        for h, note, items in sections)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{_e(title)}</title></head>'
            f'<body style="font-family:-apple-system,\'PingFang TC\',sans-serif;max-width:760px;margin:auto;'
            f'padding:16px;color:#1d1d1b;font-size:14px;line-height:1.5"><h1 style="font-size:21px">{_e(title)}</h1>'
            f'{body}</body></html>')


def build(run_started):
    """Return (subject, body_html, full_html, text, n_new)."""
    cfg = fetcher.load_config()
    today = datetime.now().strftime("%Y-%m-%d")
    url = os.environ.get("PUBLIC_URL", "http://127.0.0.1:5000")
    sections, full_sections, text_lines, counts = [], [], [], {}

    for kind, label, body_top in (("skill", "Skill", TOP_IN_BODY), ("mcp", "MCP", MCP_IN_BODY)):
        with db.conn() as c:
            items = [db.row_to_skill(r) for r in c.execute(
                "SELECT * FROM skills WHERE hidden = 0 AND kind = ?", (kind,))]
        if not items:
            continue
        ranked = [s for s in ranking.rank(items, cfg.get("interests")) if s.get("feedback") != -1]
        picks = ranking.curate_for(ranked, kind, cfg)[:body_top]
        new = [s for s in ranked if (s.get("first_seen") or "") >= run_started]
        initial = len(new) > 0.5 * len(ranked)  # first import of this kind: everything is "new"
        counts[kind] = 0 if initial else len(new)
        if initial:
            sections.append((f"今天新發現的 {label}", f"<p>這是第一次匯入 {len(ranked)} 個 {label}，請直接看下方精選。</p>", []))
        elif new:
            sections.append((f"今天新發現的 {label}：{len(new)} 個", "", new[:100]))
            text_lines += [f"[{label}] {s['name']}: {(s.get('summary_zh') or s.get('description') or '')[:60]} {s['url']}"
                           for s in new[:100]]
        else:
            sections.append((f"今天新發現的 {label}", "<p>今天沒有新的。</p>", []))
        sections.append((f"精選 {label} {len(picks)} 個（為你推薦）", "", picks))
        full_sections.append((f"全部 {label} {len(ranked)} 個（為你排序）", "", ranked))

    head = (f'<p>依你的 👍/👎 和興趣排序；在網頁 <a href="{_e(url)}">{_e(url)}</a> 按讚會讓明天的推薦更準。'
            '完整排序的全部清單在附件 all-skills.html。</p>')
    body_html = _page(f"Skill Explorer 日報 {today}", [(sections[0][0], head + sections[0][1], sections[0][2])] + sections[1:])
    full_html = _page(f"全部 Skill / MCP（為你排序）{today}", full_sections)
    n_new = sum(counts.values())
    text = "\n".join([f"Skill Explorer 日報 {today}",
                      f"今天新增 skill {counts.get('skill', 0)} 個、MCP {counts.get('mcp', 0)} 個", ""]
                     + text_lines + ["", f"完整內容請看 HTML 版或 {url}"])
    parts = [f"{k} {v} 個" for k, v in (("skill", counts.get("skill", 0)), ("MCP", counts.get("mcp", 0))) if v]
    subject = f"[Skill Explorer] {today} 新增 " + "、".join(parts) if parts else f"[Skill Explorer] {today} 日報（無新增）"
    return subject, body_html, full_html, text, n_new


def save_and_send(run_started, log):
    subject, body_html, full_html, text, n_new = build(run_started)
    ddir = db.DB_PATH.parent / "digests"
    ddir.mkdir(exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    (ddir / f"{day}.html").write_text(body_html, encoding="utf-8")
    (ddir / f"{day}-all.html").write_text(full_html, encoding="utf-8")

    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not (host and to):
        log("digest saved (email not configured: set SMTP_HOST / MAIL_TO)")
        return False
    if os.environ.get("SMTP_USER") and not os.environ.get("SMTP_PASSWORD"):
        log("digest saved, not emailed: SMTP_PASSWORD 未設定（Gmail 需要應用程式密碼）")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or to
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    msg.add_attachment(full_html.encode("utf-8"), maintype="text", subtype="html", filename="all-skills.html")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with (smtplib.SMTP_SSL(host, port, timeout=60) if port == 465 else smtplib.SMTP(host, port, timeout=60)) as s:
        if port != 465:
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
    log(f"digest emailed: {subject}")
    return True
