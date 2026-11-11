"""Builds docs/index.html: one self-contained page with every match, filters and quick decisions.

Served by GitHub Pages (Settings -> Pages -> Deploy from branch: main, folder /docs).
Decisions (saved / applied / skipped) are kept in the viewer's browser (localStorage).
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import ROOT, read_matches, read_rejected

DOCS = os.path.join(ROOT, "docs")
PAGE_PATH = os.path.join(DOCS, "index.html")

KEEP = ("first_seen", "posted", "score", "status", "category", "company", "title", "location", "workplace",
        "flags", "url", "ats", "uid", "ai", "ai_score", "ai_role", "ai_level", "ai_sponsor", "ai_why")


def _slim(r: dict, extra=()) -> dict:
    return {k: r.get(k, "") for k in KEEP + tuple(extra)}


def build_dashboard(tz_name: str, boards_total: int, ai_status: str, now: datetime | None = None,
                    profile: dict | None = None, closed: set[str] = frozenset()) -> str:
    """`profile` (the `profile:` section of a profile's config) changes the page's name, how far back it
    looks and where the viewer's marks are stored; `closed` holds matches whose postings were taken down."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(tz_name)
    p = profile or {}
    days = int(p.get("report_days", 7))
    jobs = [_slim(r) for r in read_matches(since=now - timedelta(days=max(30, days)))
            if r.get("status") in ("alert", "quiet") and r["uid"] not in closed]
    hidden = [_slim(r, ("reject_reason",)) for r in read_rejected(since=now - timedelta(days=days))]
    meta = {
        "updated": now.isoformat(),
        "updated_local": now.astimezone(tz).strftime("%a %b %-d, %-I:%M %p %Z"),
        "boards": boards_total,
        "ai": ai_status,
    }
    data = json.dumps({"meta": meta, "jobs": jobs, "hidden": hidden}, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    page = TEMPLATE
    if p:
        label = html.escape(p.get("label", "Job Radar"))
        page = page.replace("<title>Job Radar</title>", f"<title>{label}</title>")
        page = page.replace("<h1>Job Radar</h1>", f"<h1>{label}</h1>")
        page = page.replace('"jobradar.marks.v1"', json.dumps(f"jobradar.{p.get('name', 'profile')}.marks.v1"))
        page = page.replace('"Refreshes about every 30 minutes."', json.dumps(p.get("refresh_note", "Refreshes every few hours.")))
        if days > 7:  # this page is a list of open jobs, not only this week's new ones
            page = page.replace('<option value="7" selected>Last 7 days</option><option value="30">Last 30 days</option>',
                                f'<option value="7">Last 7 days</option><option value="30">Last 30 days</option>'
                                '<option value="3650" selected>All open</option>')
            page = page.replace("to review (7 days)", "to review").replace("< 7 * 24 * H).length", "< 3650 * 24 * H).length")
            page = page.replace("hrs > 3 ?", "hrs > 9 ?")
        if p.get("chips"):  # which note chips are red / green / amber for this candidate
            c = p["chips"]
            old = page[page.index("function chipClass(f)"):page.index("const mark = uid")]
            new = ("function chipClass(f) { f = f.toLowerCase();\n"
                   f"  if (new RegExp({json.dumps(c.get('bad', '$^'))}).test(f)) return \"b\";\n"
                   f"  if (new RegExp({json.dumps(c.get('good', '$^'))}).test(f)) return \"g\";\n"
                   f"  if (new RegExp({json.dumps(c.get('warn', '$^'))}).test(f)) return \"w\"; return \"\"; }}\n")
            page = page.replace(old, new)
    return page.replace("/*DATA*/null", data)


def write_dashboard(html: str) -> None:
    os.makedirs(DOCS, exist_ok=True)
    tmp = PAGE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, PAGE_PATH)
    nojekyll = os.path.join(DOCS, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Radar</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#5f6b7a;--line:#e3e6eb;--acc:#2457d6;--good:#18794e;--goodbg:#e6f4ec;
--warn:#a35200;--warnbg:#fdf0e0;--bad:#b42318;--badbg:#fdecea;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--ink:#e8eaee;--mute:#9aa4b2;--line:#2a2f38;--acc:#7aa2ff;
--good:#5cd59a;--goodbg:#13281e;--warn:#f0b35e;--warnbg:#2c2113;--bad:#ff8a80;--badbg:#2e1615;--chip:#232831}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px 8px}
h1{font-size:18px;margin:0 0 2px}.sub{color:var(--mute);font-size:12.5px}
.stats{display:flex;gap:8px;margin:8px 0 6px;flex-wrap:wrap}.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:5px 10px;font-size:12.5px}
.stat b{font-size:15px;margin-right:3px}
.tabs{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;margin-top:4px}.tabs::-webkit-scrollbar{display:none}
.tab{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:5px 12px;font-size:13px;white-space:nowrap;cursor:pointer}
.tab.on{background:var(--acc);border-color:var(--acc);color:#fff}
.bar{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.bar input,.bar select{font:inherit;font-size:13.5px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
.bar input[type=search]{flex:1 1 180px;min-width:140px}
label.ck{font-size:13px;display:flex;align-items:center;gap:4px;color:var(--mute)}
main{max-width:860px;margin:0 auto;padding:10px 12px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:11px 12px;margin:9px 0;display:flex;gap:11px}
.card.done{opacity:.55}
.score{flex:0 0 44px;height:44px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px}
.s-hi{background:var(--goodbg);color:var(--good)}.s-mid{background:var(--warnbg);color:var(--warn)}.s-lo{background:var(--chip);color:var(--mute)}
.body{flex:1;min-width:0}
.t{font-weight:650;font-size:15.5px;color:var(--ink);text-decoration:none;display:block}.t:hover{text-decoration:underline}
.co{font-size:13.5px;margin-top:1px}.co b{font-weight:600}
.meta{color:var(--mute);font-size:12.5px;margin-top:2px}
.why{font-size:13px;margin-top:5px;color:var(--ink)}.why i{color:var(--mute);font-style:normal}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.chip{font-size:11.5px;padding:2px 7px;border-radius:999px;background:var(--chip);color:var(--mute)}
.chip.g{background:var(--goodbg);color:var(--good)}.chip.w{background:var(--warnbg);color:var(--warn)}.chip.b{background:var(--badbg);color:var(--bad)}
.new{font-size:10.5px;font-weight:700;color:#fff;background:var(--acc);border-radius:5px;padding:1px 5px;margin-left:6px;vertical-align:2px}
.acts{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.btn{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer;text-decoration:none}
.btn.p{background:var(--acc);border-color:var(--acc);color:#fff}.btn.on{border-color:var(--acc);color:var(--acc);font-weight:600}
.empty{color:var(--mute);text-align:center;padding:40px 10px}
.more{display:block;margin:14px auto;padding:8px 16px}
footer{color:var(--mute);font-size:12px;text-align:center;padding:10px}
@media (max-width:640px){header{position:static}.btn{padding:5px 8px;font-size:12.5px}.acts{gap:5px}}
details.bk{margin-top:18px;font-size:13px;color:var(--mute)}details.bk textarea{width:100%;height:70px;font:12px monospace;margin-top:6px}
</style>
</head>
<body>
<header>
  <h1>Job Radar</h1>
  <div class="sub" id="sub"></div>
  <div class="stats" id="stats"></div>
  <div class="tabs" id="tabs"></div>
  <div class="bar">
    <input type="search" id="q" placeholder="Search title, company, location">
    <select id="when"><option value="1">Last 24h</option><option value="3">Last 3 days</option><option value="7" selected>Last 7 days</option><option value="30">Last 30 days</option></select>
    <select id="sort"><option value="score">Best match</option><option value="new">Newest</option></select>
    <select id="wp"><option value="">Any work type</option><option value="onsite">On-site</option><option value="hybrid">Hybrid</option><option value="remote">Remote</option></select>
    <label class="ck"><input type="checkbox" id="spon"> Sponsor-friendly</label>
    <label class="ck"><input type="checkbox" id="strong"> Strong only</label>
  </div>
</header>
<main>
  <div id="list"></div>
  <details class="bk"><summary>Move your saved/applied marks to another device</summary>
    Marks are stored in this browser. Copy them here and paste on the other device.
    <textarea id="bk"></textarea>
    <button class="btn" id="bkcopy">Copy</button> <button class="btn" id="bkload">Load pasted marks</button> <span id="bkmsg"></span>
  </details>
</main>
<footer id="foot"></footer>
<script>
const DATA = /*DATA*/null;
const KEY = "jobradar.marks.v1";
let marks = {};
try { marks = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { marks = {}; }
function saveMarks() { try { localStorage.setItem(KEY, JSON.stringify(marks)); } catch (e) {} }
const now = Date.now();
const H = 3600e3;
const jobs = DATA.jobs.map(j => ({...j, score: +j.score || 0, ai_score: j.ai_score === "" ? null : +j.ai_score,
  t: Date.parse(j.posted || j.first_seen), seen: Date.parse(j.first_seen)}));
const hidden = DATA.hidden.map(j => ({...j, score: +j.score || 0, t: Date.parse(j.posted || j.first_seen), seen: Date.parse(j.first_seen)}));
let tab = "review", shown = 60;
const $ = id => document.getElementById(id);
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function ago(t) { if (!t) return ""; const h = (now - t) / H; if (h < 1) return Math.max(1, Math.round(h * 60)) + "m ago";
  if (h < 48) return Math.round(h) + "h ago"; return Math.round(h / 24) + "d ago"; }
function sponsorish(j) { const f = (j.flags + " " + j.ai_sponsor).toLowerCase(); return /sponsors visa|e-verify|cap-exempt|\byes\b/.test(f) && !/no future h-1b|no sponsorship/.test(f); }
function chipClass(f) { f = f.toLowerCase();
  if (/no (future )?h-1b|no sponsorship|clearance|citizen|too senior|not a target|5\+|staffing/.test(f)) return "b";
  if (/sponsor|e-verify|cap-exempt|entry|0-2|≤2|junior|new grad|priority/.test(f)) return "g";
  if (/3\+|4\+|3-4|repost|unclear|remote/.test(f)) return "w"; return ""; }
const mark = uid => (marks[uid] || {}).s || "";
function tabsDef() {
  const c = s => jobs.filter(j => mark(j.uid) === s).length;
  return [["review", "To review", jobs.filter(j => !mark(j.uid)).length], ["saved", "Saved", c("saved")],
          ["applied", "Applied", c("applied")], ["skipped", "Skipped", c("skipped")], ["hidden", "Filtered out", hidden.length]];
}
function renderTabs() {
  const cur = filtered().length;
  $("tabs").innerHTML = tabsDef().map(([k, l, n]) => [k, l, k === tab ? cur : n]).map(([k, l, n]) => `<button class="tab ${k === tab ? "on" : ""}" data-t="${k}">${l} · ${n}</button>`).join("");
}
function filtered() {
  const q = $("q").value.trim().toLowerCase(), days = +$("when").value, wp = $("wp").value;
  const src = tab === "hidden" ? hidden : jobs.filter(j => tab === "review" ? !mark(j.uid) : mark(j.uid) === tab);
  let out = src.filter(j => {
    if (tab === "review" && now - (j.t || j.seen) > days * 24 * H) return false;
    if (q && !(j.title + " " + j.company + " " + j.location + " " + (j.ai_why || "")).toLowerCase().includes(q)) return false;
    if (wp && j.workplace !== wp) return false;
    if ($("spon").checked && !sponsorish(j)) return false;
    if ($("strong").checked && j.status !== "alert") return false;
    return true; });
  out.sort($("sort").value === "new" ? (a, b) => (b.t || b.seen) - (a.t || a.seen) : (a, b) => b.score - a.score || (b.t || 0) - (a.t || 0));
  return out;
}
function card(j) {
  const s = j.score, cls = s >= 60 ? "s-hi" : s >= 45 ? "s-mid" : "s-lo", m = mark(j.uid);
  const flags = (j.flags || "").split(/;\s*/).filter(Boolean);
  if (j.ai_level) flags.unshift(j.ai_level + " level");
  if (j.reject_reason) flags.unshift(j.reject_reason);
  const fresh = !m && now - j.seen < 24 * H && tab !== "hidden";
  const posted = j.posted ? "posted " + ago(j.t) : "found " + ago(j.seen);
  const why = j.ai_why ? `<div class="why"><i>AI:</i> ${esc(j.ai_why)}${j.ai_score != null ? ` <i>(${j.ai_score})</i>` : ""}</div>` : "";
  const btn = (k, label) => `<button class="btn ${m === k ? "on" : ""}" data-u="${esc(j.uid)}" data-m="${k}">${label}</button>`;
  return `<div class="card ${m === "skipped" || m === "applied" ? "done" : ""}">
    <div class="score ${cls}" title="Match score">${s}</div>
    <div class="body">
      <a class="t" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
      <div class="co"><b>${esc(j.company)}</b>${fresh ? '<span class="new">NEW</span>' : ""}</div>
      <div class="meta">${esc(j.location || "Location not listed")}${j.workplace ? " · " + esc(j.workplace) : ""} · ${posted} · ${esc(j.ats)}</div>
      ${why}
      <div class="chips">${flags.map(f => `<span class="chip ${chipClass(f)}">${esc(f)}</span>`).join("")}</div>
      ${tab === "hidden" ? `<div class="acts"><a class="btn" href="${esc(j.url)}" target="_blank" rel="noopener">Open ↗</a></div>` :
      `<div class="acts"><a class="btn p" href="${esc(j.url)}" target="_blank" rel="noopener">Apply ↗</a>${btn("saved", "★ Save")}${btn("applied", "✓ Applied")}${btn("skipped", "✕ Skip")}</div>`}
    </div></div>`;
}
function render() {
  renderTabs();
  const list = filtered();
  $("list").innerHTML = list.length ? list.slice(0, shown).map(card).join("") +
    (list.length > shown ? `<button class="btn more" id="more">Show more (${list.length - shown} left)</button>` : "")
    : `<div class="empty">${tab === "review" ? "Nothing new to review with these filters." : "Nothing here yet."}</div>`;
}
function header() {
  const day = jobs.filter(j => now - (j.t || j.seen) < 24 * H), strong = day.filter(j => j.status === "alert");
  const todo = jobs.filter(j => !mark(j.uid) && now - (j.t || j.seen) < 7 * 24 * H).length;
  $("sub").textContent = `Updated ${DATA.meta.updated_local} · ${(+DATA.meta.boards).toLocaleString()} job boards watched · AI check: ${DATA.meta.ai}`;
  $("stats").innerHTML = `<div class="stat"><b>${day.length}</b>new in 24h</div><div class="stat"><b>${strong.length}</b>strong in 24h</div>` +
    `<div class="stat"><b>${todo}</b>to review (7 days)</div><div class="stat"><b>${jobs.filter(j => mark(j.uid) === "applied").length}</b>applied</div>`;
  const hrs = (now - Date.parse(DATA.meta.updated)) / H;
  $("foot").textContent = hrs > 3 ? `Data is ${Math.round(hrs)} hours old: check the radar's GitHub Actions runs.` : "Refreshes about every 30 minutes.";
}
document.addEventListener("click", e => {
  const t = e.target.closest("[data-t]"); if (t) { tab = t.dataset.t; shown = 60; render(); return; }
  const b = e.target.closest("[data-m]");
  if (b) { const u = b.dataset.u, m = b.dataset.m; if (mark(u) === m) delete marks[u]; else marks[u] = {s: m, t: new Date().toISOString()};
    saveMarks(); header(); render(); return; }
  if (e.target.id === "more") { shown += 60; render(); }
});
["q", "when", "sort", "wp", "spon", "strong"].forEach(id => $(id).addEventListener("input", () => { shown = 60; render(); }));
$("bk").addEventListener("focus", () => { $("bk").value = JSON.stringify(marks); });
$("bkcopy").onclick = async () => { $("bk").value = JSON.stringify(marks); try { await navigator.clipboard.writeText($("bk").value); $("bkmsg").textContent = "Copied."; } catch (e) { $("bk").select(); $("bkmsg").textContent = "Select and copy the text."; } };
$("bkload").onclick = () => { try { const m = JSON.parse($("bk").value); if (typeof m !== "object" || !m) throw 0; Object.assign(marks, m); saveMarks(); header(); render(); $("bkmsg").textContent = "Loaded."; } catch (e) { $("bkmsg").textContent = "That text isn't a marks backup."; } };
header(); render();
</script>
</body>
</html>
"""
