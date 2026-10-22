#!/usr/bin/env python3
"""Page with everything Claude Code remembers about you: every auto-memory record
(~/.claude/projects/*/memory, plus what memory-to-repo already archived) with creation/change dates,
the MEMORY.md line Claude sees at session start, links between records, the session that wrote it,
search/filters and marks (keep / move to repo / delete) that copy out as a list for Claude.
Stdlib only; Linux, macOS, Windows.

  memview.py                    build ~/.claude/memory-archive/memory.html and open it in the browser
  memview.py --out FILE         build into FILE instead
  memview.py --no-open          only build
  memview.py --watch            rebuild whenever a memory file changes (for a background service)
"""
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memtool import ARCHIVE, PROJECTS, guess_path, real_path  # noqa: E402

OUT = ARCHIVE / "memory.html"
CACHE = ARCHIVE / ".memview-sessions.json"
LINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
INDEX_LINE_RE = re.compile(r"^\s*-\s*\[([^\]]*)\]\(([^)]+)\)\s*(?:[—–-]\s*(.*))?$")
TITLE_KEY = b'"aiTitle":"'
ARCHIVED_SUFFIX = re.compile(r"-\d{14}$")


def project_label(pid):
    path = real_path(PROJECTS / pid) or guess_path(pid) or pid
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta, nested = {}, None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(\s*)([\w-]+):\s*(.*)$", line)
        if not m:
            continue
        indent, key, val = m.groups()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if indent and nested is not None:
            meta[nested + "." + key] = val
        else:
            nested = key if not val else None
            if val:
                meta[key] = val
    return meta, text[end + 4:].lstrip("\n")


def session_info(wanted):
    """{session_id: [original memory paths]} -> {session_id: {"title", "born": {path: iso timestamp}}}.
    The title is the session's latest aiTitle; "born" is when that session first wrote each file —
    the real creation time, since a full rewrite resets the file's own birth time.
    Cached by transcript (path, size, mtime): transcripts are big."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out, fresh = {}, {}
    for sid, paths in wanted.items():
        p = next(iter(PROJECTS.glob(f"*/{sid}.jsonl")), None)
        if not p:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        key = f"{p}:{st.st_size}:{int(st.st_mtime)}:{','.join(sorted(paths))}"
        hit = cache.get(sid)
        if hit and hit.get("key") == key:
            info = hit["info"]
        else:
            info = {"title": "", "born": {}}
            needles = {path: ('"file_path":' + json.dumps(path)).encode() for path in paths}
            with open(p, "rb") as f:
                for line in f:
                    i = line.find(TITLE_KEY)
                    if i >= 0:
                        try:
                            info["title"] = json.JSONDecoder().raw_decode(line[i + len(TITLE_KEY) - 1:].decode("utf-8", "replace"))[0]
                        except ValueError:
                            pass
                    for path, needle in needles.items():
                        if path not in info["born"] and needle in line:
                            m = re.search(rb'"timestamp":"([^"]+)"', line)
                            if m:
                                info["born"][path] = m.group(1).decode()
        fresh[sid] = {"key": key, "info": info}
        out[sid] = info
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return out


def birth_time(path, st):
    """Creation time: macOS has st_birthtime, Windows keeps it in st_ctime, Linux needs statx via stat(1)."""
    if getattr(st, "st_birthtime", None):
        return st.st_birthtime
    if os.name == "nt":
        return st.st_ctime
    try:
        ts = subprocess.run(["stat", "-c", "%W", str(path)], capture_output=True, text=True).stdout.strip()
        if ts.isdigit() and int(ts) > 0:
            return int(ts)
    except OSError:
        pass
    return min(st.st_mtime, st.st_ctime)


def stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def memory_dirs():
    """(dir, project id, archived) for live memory and for what memory-to-repo moved to the archive."""
    for mdir in sorted(PROJECTS.glob("*/memory")):
        if mdir.is_dir():
            yield mdir, mdir.parent.name, False
    if ARCHIVE.is_dir():
        for adir in sorted(ARCHIVE.iterdir()):
            if adir.is_dir() and not adir.name.startswith("."):
                yield adir, ARCHIVED_SUFFIX.sub("", adir.name), True


def collect():
    records, problems, projects = [], [], {}
    for mdir, pid, archived in memory_dirs():
        files = sorted(p for p in mdir.glob("*.md") if p.name != "MEMORY.md")
        index = mdir / "MEMORY.md"
        index_lines = {}
        if index.exists():
            for n, line in enumerate(index.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                m = INDEX_LINE_RE.match(line)
                if m:
                    index_lines[m.group(2).strip()] = {"title": m.group(1), "hook": (m.group(3) or "").strip(), "line": n}
        if not files and not index_lines:
            continue
        if pid not in projects:
            projects[pid] = {"id": pid, "label": project_label(pid)}
        names, mine = {}, []
        for p in files:
            st = p.stat()
            meta, body = frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            created = birth_time(p, st)
            rec = {
                "id": f"{'archive:' if archived else ''}{pid}/{p.name}",
                "project": pid,
                "archived": archived,
                "file": p.name,
                "path": str(p),
                "origin": str(PROJECTS / pid / "memory" / p.name),
                "name": meta.get("name") or p.stem,
                "description": meta.get("description", ""),
                "type": meta.get("metadata.type") or meta.get("type") or "",
                "session": meta.get("metadata.originSessionId") or meta.get("originSessionId") or "",
                "created": stamp(created),
                "modified": stamp(st.st_mtime),
                "edited": st.st_mtime - created > 120,
                "_created": created,
                "_mtime": st.st_mtime,
                "size": len(body),
                "body": body,
                "index": index_lines.get(p.name),
                "links": sorted(set(LINK_RE.findall(body))),
            }
            mine.append(rec)
            names[rec["name"]] = rec["id"]
            names.setdefault(p.stem, rec["id"])
        for target, info in index_lines.items():
            if "/" not in target and not target.startswith("http") and not (mdir / target).exists():
                problems.append({"project": pid, "text": f"MEMORY.md:{info['line']} ссылается на {target}, а файла нет"})
        for rec in mine:
            rec["resolved"] = {link: names.get(link) for link in rec["links"]}
        records += mine
    by_id = {r["id"]: r for r in records}
    for r in records:
        r["backlinks"] = []
    for r in records:
        for target in r["resolved"].values():
            if target and target != r["id"]:
                by_id[target]["backlinks"].append(r["id"])
    wanted = {}
    for r in records:
        if r["session"]:
            wanted.setdefault(r["session"], []).append(r["origin"])
    sessions = session_info(wanted)
    for r in records:
        info = sessions.get(r["session"]) or {}
        r["sessionTitle"] = info.get("title", "")
        born = (info.get("born") or {}).get(r["origin"])
        created, mtime = r.pop("_created"), r.pop("_mtime")
        if born:
            ts = datetime.fromisoformat(born.replace("Z", "+00:00")).timestamp()
            if ts < created:
                r["created"] = stamp(ts)
                r["edited"] = mtime - ts > 120
    return records, problems, sorted(projects.values(), key=lambda p: p["label"])


PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Память Claude</title>
<style>
:root {
  color-scheme: light;
  --bg: #f7f7f5; --surface: #ffffff; --fg: #0b0b0b; --muted: #52514e; --faint: #8a8985;
  --line: #e8e7e3; --chip: #f1f0ec; --hover: #f5f4f1; --accent: #2a78d6; --accent-ink: #1c5cab;
  --sel: #eaf2fd; --warn: #b25c00; --warn-bg: #fdf1e2;
  --t-user: #7a5af5; --t-feedback: #eb6834; --t-project: #2a78d6; --t-reference: #1baf7a; --t-none: #8a8985;
  --m-keep: #1baf7a; --m-repo: #2a78d6; --m-del: #e34948;
  --shadow: 0 1px 2px rgba(20,20,15,.04), 0 4px 16px rgba(20,20,15,.04);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --fg: #f2f2f0; --muted: #c3c2b7; --faint: #8d8c85;
    --line: #2a2a28; --chip: #232321; --hover: #212120; --accent: #3987e5; --accent-ink: #86b6ef;
    --sel: #1b2a3d; --warn: #e8a24a; --warn-bg: #2e2416;
    --t-user: #9085e9; --t-feedback: #d95926; --t-project: #3987e5; --t-reference: #199e70; --t-none: #8d8c85;
    --m-keep: #199e70; --m-repo: #3987e5; --m-del: #e66767;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--fg); height: 100%; }
body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; }
button { font: inherit; color: inherit; }
.num { font-variant-numeric: tabular-nums; }
.app { display: grid; grid-template-columns: 250px minmax(420px, 1fr) minmax(420px, 1.15fr); height: 100vh; }
aside, .listcol, .detail { min-height: 0; overflow: auto; }
aside { border-right: 1px solid var(--line); padding: 20px 14px 40px; }
.listcol { border-right: 1px solid var(--line); display: flex; flex-direction: column; }
.detail { padding: 26px 32px 60px; }

h1 { font-size: 20px; margin: 0 0 2px; letter-spacing: -0.02em; }
.updated { color: var(--faint); font-size: 12px; margin-bottom: 16px; }
.tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 18px; }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; box-shadow: var(--shadow); }
.tile .v { font-size: 20px; font-weight: 650; line-height: 1.1; }
.tile .k { color: var(--muted); font-size: 11.5px; }
.tile.warn .v { color: var(--warn); }
.side-h { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--faint); margin: 16px 0 6px; font-weight: 600; }
.frow { display: flex; align-items: center; gap: 8px; width: 100%; border: 0; background: none; text-align: left;
  padding: 4px 6px; border-radius: 6px; cursor: pointer; font-size: 12.5px; }
.frow:hover { background: var(--hover); }
.frow[aria-pressed="true"] { background: var(--sel); }
.frow .lbl { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.frow .cnt { color: var(--faint); font-size: 12px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.export { margin-top: 18px; display: flex; flex-direction: column; gap: 6px; }
.btn { border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 12.5px; color: var(--muted); }
.btn:hover { color: var(--fg); border-color: var(--faint); }
.hint { color: var(--faint); font-size: 11.5px; line-height: 1.5; margin-top: 14px; }
kbd { font: 10.5px ui-monospace, monospace; color: var(--faint); border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 4px; padding: 0 4px; background: var(--surface); }

.toolbar { position: sticky; top: 0; z-index: 2; background: var(--bg); padding: 14px 14px 10px; border-bottom: 1px solid var(--line); display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.toolbar input { flex: 1 1 200px; height: 34px; padding: 0 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--fg); font: inherit; }
.toolbar input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--surface); height: 34px; }
.seg button { border: 0; background: none; padding: 0 10px; cursor: pointer; color: var(--muted); font-size: 12.5px; }
.seg button[aria-pressed="true"] { background: var(--chip); color: var(--fg); font-weight: 600; }
.count { color: var(--faint); font-size: 12px; width: 100%; }
.rows { padding: 4px 0 40px; }
.group { position: sticky; top: 0; font-size: 11.5px; font-weight: 600; color: var(--muted); padding: 10px 14px 4px; }
.row { display: grid; grid-template-columns: 10px minmax(0, 1fr) auto; gap: 4px 10px; padding: 8px 14px; cursor: pointer; border-left: 3px solid transparent; }
.row:hover { background: var(--hover); }
.row.sel { background: var(--sel); }
.row[data-mark="keep"] { border-left-color: var(--m-keep); }
.row[data-mark="repo"] { border-left-color: var(--m-repo); }
.row[data-mark="del"] { border-left-color: var(--m-del); }
.row[data-mark="del"] .rt { text-decoration: line-through; color: var(--faint); }
.row .dot { margin-top: 6px; }
.rt { font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rd { grid-column: 2 / 4; color: var(--muted); font-size: 12.5px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.rm { color: var(--faint); font-size: 11.5px; white-space: nowrap; text-align: right; }
.rp { grid-column: 2 / 4; color: var(--faint); font-size: 11.5px; display: flex; gap: 8px; flex-wrap: wrap; }
.arch { color: var(--muted); background: var(--chip); border-radius: 4px; padding: 0 5px; font-size: 11px; }
.row.archived .rt { color: var(--muted); }
.flag { color: var(--warn); background: var(--warn-bg); border-radius: 4px; padding: 0 5px; font-size: 11px; }

.detail h2 { font-size: 22px; margin: 0 0 6px; letter-spacing: -0.01em; overflow-wrap: anywhere; }
.d-desc { color: var(--muted); font-size: 14.5px; margin-bottom: 14px; }
.meta { display: grid; grid-template-columns: 130px 1fr; gap: 4px 12px; font-size: 12.5px; margin-bottom: 14px; }
.meta dt { color: var(--faint); }
.meta dd { margin: 0; overflow-wrap: anywhere; }
.meta code, .body code { font: 12px ui-monospace, monospace; background: var(--chip); padding: 1px 5px; border-radius: 4px; }
.copy { border: 0; background: none; color: var(--accent-ink); cursor: pointer; padding: 0; font-size: 12px; margin-left: 6px; }
.marks { display: flex; gap: 6px; margin: 4px 0 18px; }
.marks button { border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 5px 12px; cursor: pointer; font-size: 12.5px; }
.marks button[data-m="keep"][aria-pressed="true"] { border-color: var(--m-keep); color: var(--m-keep); font-weight: 600; }
.marks button[data-m="repo"][aria-pressed="true"] { border-color: var(--m-repo); color: var(--m-repo); font-weight: 600; }
.marks button[data-m="del"][aria-pressed="true"] { border-color: var(--m-del); color: var(--m-del); font-weight: 600; }
.note { width: 100%; min-height: 34px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--fg); font: inherit; font-size: 12.5px; padding: 6px 8px; resize: vertical; margin: -8px 0 18px; }
.body { border-top: 1px solid var(--line); padding-top: 14px; font-size: 14px; line-height: 1.6; max-width: 76ch; }
.body p { margin: 0 0 10px; }
.body ul { margin: 0 0 10px; padding-left: 20px; }
.body h3, .body h4 { margin: 16px 0 6px; font-size: 15px; }
.body a, .lnk { color: var(--accent-ink); text-decoration: none; cursor: pointer; }
.body a:hover, .lnk:hover { text-decoration: underline; }
.body .broken { color: var(--warn); }
.rel { margin-top: 18px; font-size: 12.5px; }
.rel b { color: var(--faint); font-weight: 600; margin-right: 6px; }
.empty { color: var(--faint); padding: 40px 0; text-align: center; }
.problems { margin: 0 0 6px; padding-left: 16px; color: var(--warn); font-size: 11.5px; }
#toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--fg); color: var(--bg); padding: 8px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .15s; pointer-events: none; }
#toast.show { opacity: 1; }
@media (max-width: 1100px) { .app { grid-template-columns: 220px 1fr; } .detail { grid-column: 1 / 3; border-top: 1px solid var(--line); } .app { height: auto; } }
</style>
</head>
<body>
<div class="app">
  <aside>
    <h1>Память Claude</h1>
    <div class="updated" id="updated"></div>
    <div class="tiles" id="tiles"></div>
    <div class="side-h">Где</div>
    <div id="wheref"></div>
    <div class="side-h">Отметки</div>
    <div id="markf"></div>
    <div class="side-h">Тип</div>
    <div id="typef"></div>
    <div class="side-h">Проект</div>
    <div id="projf"></div>
    <div class="export">
      <button class="btn" id="exp">Скопировать отметки для Claude</button>
      <button class="btn" id="clr">Сбросить отметки</button>
    </div>
    <div class="hint">
      <kbd>/</kbd> поиск · <kbd>j</kbd>/<kbd>k</kbd> вверх-вниз<br>
      <kbd>1</kbd> оставить · <kbd>2</kbd> в репо · <kbd>3</kbd> удалить · <kbd>0</kbd> снять<br>
      Отметки хранятся в этом браузере. Список вставь Claude — например, в /memory-to-repo.
    </div>
  </aside>
  <section class="listcol">
    <div class="toolbar">
      <input id="q" placeholder="Поиск по названию, описанию, тексту" autocomplete="off">
      <div class="seg" id="sort">
        <button data-s="created">Создана</button><button data-s="modified">Изменена</button><button data-s="name">А–Я</button>
      </div>
      <div class="count" id="count"></div>
    </div>
    <div class="rows" id="rows"></div>
  </section>
  <section class="detail" id="detail"><div class="empty">Выбери запись слева</div></section>
</div>
<div id="toast"></div>
<script>
const DATA = __DATA__;
const PROBLEMS = __PROBLEMS__;
const PROJECTS = __PROJECTS__;
const GENERATED = __GENERATED__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const TYPES = { user: "о тебе", feedback: "поправки", project: "проект", reference: "ссылки", "": "без типа" };
const MARKS = { keep: "оставить", repo: "в репо", del: "удалить" };
const byId = Object.fromEntries(DATA.map(r => [r.id, r]));
const projLabel = Object.fromEntries(PROJECTS.map(p => [p.id, p.label]));

const KEY = "memory-index";
const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
const st = Object.assign({ q: "", type: "", project: "", mark: "", where: "", sort: "created", sel: "", marks: {}, notes: {} }, saved);
st.q = "";
const save = () => localStorage.setItem(KEY, JSON.stringify({ where: st.where, type: st.type, project: st.project, mark: st.mark, sort: st.sort, sel: st.sel, marks: st.marks, notes: st.notes }));

const flagsOf = r => {
  const f = [];
  if (!r.index && !r.archived) f.push("нет в MEMORY.md");
  const broken = Object.entries(r.resolved || {}).filter(([, v]) => !v).map(([k]) => k);
  if (broken.length) f.push(`битые ссылки: ${broken.length}`);
  if (!r.type) f.push("без типа");
  return f;
};
const ago = d => {
  const days = Math.round((new Date(GENERATED.slice(0, 10)) - new Date(d.slice(0, 10))) / 864e5);
  return days === 0 ? "сегодня" : days === 1 ? "вчера" : `${days} дн. назад`;
};
const date = d => d.slice(0, 10);

function filtered() {
  const q = st.q.trim().toLowerCase();
  let rows = DATA.filter(r =>
    (!st.where || (st.where === "archive") === r.archived) &&
    (!st.type || (st.type === "-" ? !r.type : r.type === st.type)) &&
    (!st.project || r.project === st.project) &&
    (!st.mark || (st.mark === "none" ? !st.marks[r.id] : st.mark === "flags" ? flagsOf(r).length : st.marks[r.id] === st.mark)) &&
    (!q || [r.name, r.description, r.body, r.file, r.index?.hook].join("\n").toLowerCase().includes(q)));
  const k = st.sort;
  rows.sort((a, b) => k === "name" ? a.name.localeCompare(b.name, "ru") : b[k].localeCompare(a[k]));
  return rows;
}

let shown = [];
function render() {
  shown = filtered();
  $("count").textContent = `${shown.length} из ${DATA.length}`;
  document.querySelectorAll("#sort button").forEach(b => b.setAttribute("aria-pressed", b.dataset.s === st.sort));
  let html = "", lastGroup = "";
  shown.forEach((r, i) => {
    if (st.sort !== "name") {
      const g = date(r[st.sort]);
      if (g !== lastGroup) { html += `<div class="group">${g} · ${ago(g)}</div>`; lastGroup = g; }
    }
    const flags = flagsOf(r).map(f => `<span class="flag">${esc(f)}</span>`).join("");
    const when = st.sort === "modified" ? r.modified.slice(11) : r.created.slice(11);
    html += `<div class="row${r.id === st.sel ? " sel" : ""}${r.archived ? " archived" : ""}" data-i="${i}" data-mark="${st.marks[r.id] || ""}">
      <span class="dot" style="background:var(--t-${r.type || "none"})"></span>
      <div class="rt">${esc(r.name)}</div><div class="rm num">${when}</div>
      <div class="rd">${esc(r.description || r.index?.hook || "")}</div>
      <div class="rp">${r.archived ? `<span class="arch">в архиве</span>` : ""}<span>${esc(projLabel[r.project])}</span>${r.edited && st.sort === "created" ? `<span>изм. ${date(r.modified)}</span>` : ""}${st.notes[r.id] ? "<span>✎ заметка</span>" : ""}${flags}</div>
    </div>`;
  });
  $("rows").innerHTML = html || `<div class="empty">Ничего не найдено</div>`;
  renderSide();
}

function frow(label, cnt, pressed, attrs, color) {
  return `<button class="frow" ${attrs} aria-pressed="${pressed}">${color ? `<span class="dot" style="background:${color}"></span>` : ""}<span class="lbl">${esc(label)}</span><span class="cnt num">${cnt}</span></button>`;
}
function renderSide() {
  const marked = Object.values(st.marks);
  const archived = DATA.filter(r => r.archived).length;
  const flagged = DATA.filter(r => flagsOf(r).length).length;
  const week = DATA.filter(r => (new Date(GENERATED.slice(0, 10)) - new Date(r.created.slice(0, 10))) / 864e5 < 7).length;
  const old = DATA.filter(r => (new Date(GENERATED.slice(0, 10)) - new Date(r.modified.slice(0, 10))) / 864e5 > 14).length;
  $("tiles").innerHTML = [
    ["В памяти сейчас", DATA.length - archived], ["В архиве", archived], ["Проектов", PROJECTS.length], ["Новых за неделю", week],
    ["Не менялись 14+ дней", old], ["Отмечено", `${marked.length}`], ["С проблемами", flagged + PROBLEMS.length, flagged + PROBLEMS.length > 0],
  ].map(([k, v, w]) => `<div class="tile${w ? " warn" : ""}"><div class="v num">${v}</div><div class="k">${k}</div></div>`).join("");
  $("wheref").innerHTML = frow("все", DATA.length, !st.where, `data-f="where" data-v=""`) +
    frow("в памяти сейчас", DATA.length - archived, st.where === "live", `data-f="where" data-v="live"`) +
    frow("в архиве", archived, st.where === "archive", `data-f="where" data-v="archive"`);
  const cnt = m => marked.filter(x => x === m).length;
  $("markf").innerHTML =
    frow("все", DATA.length, !st.mark, `data-f="mark" data-v=""`) +
    frow("не разобраны", DATA.length - marked.length, st.mark === "none", `data-f="mark" data-v="none"`) +
    Object.entries(MARKS).map(([m, l]) => frow(l, cnt(m), st.mark === m, `data-f="mark" data-v="${m}"`, `var(--m-${m})`)).join("") +
    frow("с проблемами", flagged, st.mark === "flags", `data-f="mark" data-v="flags"`, "var(--warn)");
  const tc = t => DATA.filter(r => (r.type || "") === t).length;
  $("typef").innerHTML = frow("все", DATA.length, !st.type, `data-f="type" data-v=""`) +
    Object.entries(TYPES).filter(([t]) => tc(t)).map(([t, l]) => frow(l, tc(t), st.type === (t || "-"), `data-f="type" data-v="${t || "-"}"`, `var(--t-${t || "none"})`)).join("");
  const pc = p => DATA.filter(r => r.project === p).length;
  $("projf").innerHTML = frow("все", DATA.length, !st.project, `data-f="project" data-v=""`) +
    [...PROJECTS].sort((a, b) => pc(b.id) - pc(a.id)).map(p => frow(p.label, pc(p.id), st.project === p.id, `data-f="project" data-v="${esc(p.id)}"`)).join("");
}

function md(src, r) {
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
    .replace(/\[\[([^\]]+)\]\]/g, (_, n) => {
      const id = r.resolved?.[n.replace(/&amp;/g, "&")];
      return id ? `<a class="lnk" data-go="${esc(id)}">${n}</a>` : `<span class="broken" title="такой записи нет">[[${n}]]</span>`;
    });
  let out = "", list = false, para = [];
  const flush = () => { if (para.length) { out += `<p>${para.map(inline).join("<br>")}</p>`; para = []; } };
  for (const line of src.split("\n")) {
    const li = line.match(/^\s*[-*]\s+(.*)$/), h = line.match(/^(#{1,4})\s+(.*)$/);
    if (li) { flush(); if (!list) { out += "<ul>"; list = true; } out += `<li>${inline(li[1])}</li>`; continue; }
    if (list) { out += "</ul>"; list = false; }
    if (h) { flush(); out += `<h${Math.min(4, h[1].length + 2)}>${inline(h[2])}</h${Math.min(4, h[1].length + 2)}>`; }
    else if (!line.trim()) flush();
    else para.push(line);
  }
  flush(); if (list) out += "</ul>";
  return out;
}

function detail() {
  const r = byId[st.sel];
  if (!r) { $("detail").innerHTML = `<div class="empty">Выбери запись слева</div>`; return; }
  const m = st.marks[r.id] || "";
  const flags = flagsOf(r);
  const projProblems = PROBLEMS.filter(p => p.project === r.project);
  $("detail").innerHTML = `
    <h2>${esc(r.name)}</h2>
    <div class="d-desc">${esc(r.description)}</div>
    <div class="marks">${Object.entries(MARKS).map(([k, l], i) => `<button data-m="${k}" aria-pressed="${m === k}">${i + 1} · ${l}</button>`).join("")}</div>
    <textarea class="note" id="note" placeholder="Заметка для Claude: куда перенести, что поправить…">${esc(st.notes[r.id] || "")}</textarea>
    <dl class="meta">
      <dt>Тип</dt><dd><span class="dot" style="background:var(--t-${r.type || "none"})"></span> ${esc(TYPES[r.type] ?? r.type)}</dd>
      <dt>Создана</dt><dd class="num">${r.created} · ${ago(r.created)}</dd>
      <dt>Изменена</dt><dd class="num">${r.edited ? `${r.modified} · ${ago(r.modified)}` : "не менялась"}</dd>
      <dt>Проект</dt><dd>${esc(projLabel[r.project])}${r.archived ? " · в архиве" : ""}</dd>
      <dt>В MEMORY.md</dt><dd>${r.index && !r.archived ? `строка ${r.index.line}: «${esc(r.index.title)}»${r.index.hook ? ` — ${esc(r.index.hook)}` : ""}` : (r.archived ? "запись в архиве — Claude её больше не видит" : `<span class="flag">нет — в начале сессии Claude её не видит</span>`)}</dd>
      ${r.session ? `<dt>Сессия</dt><dd>${esc(r.sessionTitle || "без названия")} <button class="copy" data-copy="claude --resume ${esc(r.session)}">скопировать resume</button></dd>` : ""}
      <dt>Файл</dt><dd><code>${esc(r.file)}</code> <button class="copy" data-copy="${esc(r.path)}">скопировать путь</button></dd>
      ${flags.length ? `<dt>Проблемы</dt><dd>${flags.map(f => `<span class="flag">${esc(f)}</span>`).join(" ")}</dd>` : ""}
    </dl>
    <div class="body">${md(r.body, r)}</div>
    ${r.backlinks.length ? `<div class="rel"><b>Ссылаются сюда:</b>${r.backlinks.map(id => `<a class="lnk" data-go="${esc(id)}">${esc(byId[id].name)}</a>`).join(", ")}</div>` : ""}
    ${projProblems.length ? `<div class="rel"><b>Оглавление проекта:</b><ul class="problems">${projProblems.map(p => `<li>${esc(p.text)}</li>`).join("")}</ul></div>` : ""}`;
  $("note").addEventListener("input", e => { const v = e.target.value; if (v.trim()) st.notes[r.id] = v; else delete st.notes[r.id]; save(); });
}

function select(id, scroll = true) {
  if (!byId[id]) return;
  st.sel = id; save();
  document.querySelectorAll(".row").forEach(el => el.classList.toggle("sel", shown[+el.dataset.i]?.id === id));
  if (scroll) document.querySelector(".row.sel")?.scrollIntoView({ block: "nearest" });
  detail();
}
function mark(m) {
  if (!st.sel) return;
  if (!m || st.marks[st.sel] === m) delete st.marks[st.sel]; else st.marks[st.sel] = m;
  save(); const keep = st.sel; render(); select(keep, false);
}

$("rows").addEventListener("click", e => { const el = e.target.closest(".row"); if (el) select(shown[+el.dataset.i].id, false); });
document.querySelector("aside").addEventListener("click", e => {
  const b = e.target.closest(".frow"); if (!b) return;
  st[b.dataset.f] = b.dataset.v; save(); render();
});
$("sort").addEventListener("click", e => { const b = e.target.closest("button"); if (b) { st.sort = b.dataset.s; save(); render(); } });
$("q").addEventListener("input", e => { st.q = e.target.value; render(); });
$("detail").addEventListener("click", e => {
  const go = e.target.closest("[data-go]"); if (go) { st.project = ""; st.type = ""; st.mark = ""; st.q = ""; $("q").value = ""; render(); select(go.dataset.go); return; }
  const mb = e.target.closest(".marks button"); if (mb) { mark(mb.dataset.m); return; }
  const c = e.target.closest("[data-copy]"); if (c) copy(c.dataset.copy, "Скопировано");
});
$("exp").addEventListener("click", () => {
  const ids = [...new Set([...Object.keys(st.marks), ...Object.keys(st.notes)])].filter(id => byId[id]);
  if (!ids.length) { toast("Отметок нет"); return; }
  const parts = [];
  for (const [m, title] of [["del", "Удалить"], ["repo", "Перенести в репозиторий (CLAUDE.md / docs), потом удалить из памяти"], ["keep", "Оставить"], ["", "Без отметки, но с заметкой"]]) {
    const rows = ids.filter(id => (st.marks[id] || "") === m);
    if (!rows.length) continue;
    parts.push(`## ${title}\n` + rows.map(id => `- ${byId[id].path}${st.notes[id] ? `\n  заметка: ${st.notes[id].trim().replace(/\n/g, " ")}` : ""}`).join("\n"));
  }
  copy("Мои решения по памяти Claude (отметки со страницы памяти). Если идёт /memory-to-repo — учти их при разборе; иначе выполни: «удалить» — удали запись, «в репо» — перенеси в инструкции репозитория, «оставить» — не трогай; обнови MEMORY.md этих проектов.\n\n" + parts.join("\n\n"), `Скопировано: ${ids.length}`);
});
$("clr").addEventListener("click", () => { if (confirm("Сбросить все отметки и заметки?")) { st.marks = {}; st.notes = {}; save(); render(); detail(); } });

function copy(text, msg) {
  const done = () => toast(msg);
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => fallback(text, done));
  else fallback(text, done);
}
function fallback(text, done) {
  const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
  try { document.execCommand("copy"); done(); } catch { toast("Не удалось скопировать"); } t.remove();
}
let tt; function toast(m) { const t = $("toast"); t.textContent = m; t.classList.add("show"); clearTimeout(tt); tt = setTimeout(() => t.classList.remove("show"), 1600); }

document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (e.key === "Escape") { document.activeElement.blur(); return; }
  if (typing) { if (e.key === "Enter" && document.activeElement === $("q") && shown[0]) { $("q").blur(); select(shown[0].id); } return; }
  const i = shown.findIndex(r => r.id === st.sel);
  if (e.key === "/") { e.preventDefault(); $("q").focus(); }
  else if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); if (shown[i + 1]) select(shown[i + 1].id); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); if (i > 0) select(shown[i - 1].id); }
  else if ("123".includes(e.key) && e.key.length === 1) mark(Object.keys(MARKS)[+e.key - 1]);
  else if (e.key === "0") mark("");
});

$("updated").textContent = `обновлено ${GENERATED}`;
render();
detail();
</script>
</body>
</html>
"""


def build(out):
    records, problems, projects = collect()
    page = PAGE
    for key, value in (("__DATA__", records), ("__PROBLEMS__", problems), ("__PROJECTS__", projects),
                       ("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))):
        page = page.replace(key, json.dumps(value, ensure_ascii=False).replace("</", "<\\/"))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name("." + out.name + ".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, out)


def signature():
    sig = []
    for mdir, _, _ in memory_dirs():
        try:
            sig.append((str(mdir), mdir.stat().st_mtime))
            sig += [(str(p), p.stat().st_mtime) for p in mdir.iterdir()]
        except OSError:
            pass
    return sorted(sig)


def watch(out):
    # polling, not inotify: ~/.claude/projects is gigabytes of transcripts that change every second
    last = None
    while True:
        sig = signature()
        if sig != last:
            build(out)
            last = sig
        time.sleep(5)


if __name__ == "__main__":
    args = sys.argv[1:]
    out = Path(args[args.index("--out") + 1]).expanduser() if "--out" in args else OUT
    if "--watch" in args:
        watch(out)
    build(out)
    print(out)
    if "--no-open" not in args:
        webbrowser.open(out.resolve().as_uri())
