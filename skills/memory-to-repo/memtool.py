#!/usr/bin/env python3
"""Helper for the memory-to-repo skill. Stdlib only; Linux, macOS, Windows.

  memtool.py inventory                       JSON: global CLAUDE.md, root policies, every project with auto-memory
  memtool.py status                          progress, root policies, recorded branches
  memtool.py policy <root> <owner> <publish> owner: mine|team|company|public; publish: direct|pr|none
  memtool.py obsolete "<text>"               tools/rules the human says are gone; passed to every triage
  memtool.py branch <repo> <branch> <target> a commit to publish at the end (target: branch to push to / PR base)
  memtool.py done <project-id>               move that project's memory dir to the archive, mark it done
  memtool.py cost                            minutes and API-equivalent dollars of this run (main session + subagents)
  memtool.py finish                          autoMemoryEnabled=false in ~/.claude/settings.json, mark the run finished

Nothing is deleted: processed memory goes to ~/.claude/memory-archive/<project-id>/.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS = CLAUDE / "projects"
ARCHIVE = CLAUDE / "memory-archive"
STATE = ARCHIVE / "progress.json"
SETTINGS = CLAUDE / "settings.json"
INSTRUCTION_FILES = ["CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules"]
CWD_RE = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"started": datetime.now().isoformat(timespec="seconds"), "done": {}, "finished": None}


def state_get(state, key, default):
    return state.setdefault(key, default)


def save_state(state):
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def real_path(project_dir):
    """The project's real path: the cwd recorded in its session transcripts (the dir name is lossy)."""
    for transcript in sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
        try:
            with open(transcript, "rb") as f:
                for _, line in zip(range(200), f):
                    m = CWD_RE.search(line)
                    if m:
                        return json.loads(b'"' + m.group(1) + b'"')
        except (OSError, ValueError):
            continue
    return None


def guess_path(pid):
    """Fallback when no transcript is left: rebuild the path from the dir name, checking the disk.
    Claude encodes every non-alphanumeric char as '-', so try '/', '-', '.', '_' at each one."""
    parts = pid.split("-")
    if len(parts) > 1 and len(parts[0]) == 1 and parts[1] == "":  # Windows: 'C--Users-…'
        root, parts = parts[0] + ":\\", parts[2:]
    else:
        root, parts = os.sep, parts[1:]

    def walk(i, base, name):
        if i == len(parts):
            full = os.path.join(base, name)
            return full if os.path.exists(full) else None
        tok, step = (("." + parts[i + 1], 2) if parts[i] == "" and i + 1 < len(parts) else (parts[i], 1))
        if os.path.isdir(os.path.join(base, name)):
            found = walk(i + step, os.path.join(base, name), tok)
            if found:
                return found
        for sep in "-._ ":
            found = walk(i + step, base, name + sep + tok)
            if found:
                return found
        return None

    return walk(1, root, parts[0]) if parts else None


def git(path, *args):
    try:
        res = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def policy_for(path, roots):
    """The policy of the deepest root that contains path."""
    best = None
    for root in roots:
        r = os.path.expanduser(root).rstrip(os.sep)
        if path and (path == r or path.startswith(r + os.sep)) and (best is None or len(r) > len(best)):
            best = r
    return dict(roots[next(k for k in roots if os.path.expanduser(k).rstrip(os.sep) == best)], root=best) if best else None


def top_root(path):
    """Suggested root for grouping: the first dir under home (~/Work, ~/my), or the path itself."""
    home = str(Path.home())
    if not path or not path.startswith(home + os.sep):
        return path
    return os.path.join(home, path[len(home) + 1:].split(os.sep)[0])


def global_claude():
    f = CLAUDE / "CLAUDE.md"
    real = f.resolve() if f.exists() else f
    repo = git(real.parent, "rev-parse", "--show-toplevel") if real.exists() else None
    return {"path": str(f), "real": str(real), "symlink": f.is_symlink(), "repo": repo}


def frontmatter(text):
    meta = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        for line in text[3:end if end > 0 else 0].splitlines():
            m = re.match(r"^\s*([\w-]+):\s*(.+)$", line)
            if m:
                meta.setdefault(m.group(1), m.group(2).strip().strip("\"'"))
    return meta


def inventory():
    state = load_state()
    roots = state.get("roots", {})
    out = []
    for mdir in sorted(PROJECTS.glob("*/memory")):
        files = sorted(p for p in mdir.glob("*.md") if p.name != "MEMORY.md")
        index = mdir / "MEMORY.md"
        pid = mdir.parent.name
        if not files and not index.exists():
            continue
        path = real_path(mdir.parent) or guess_path(pid)
        exists = bool(path) and os.path.isdir(path)
        repo = {}
        if exists:
            top = git(path, "rev-parse", "--show-toplevel")
            if top:
                head = git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
                repo = {
                    "root": top,
                    "branch": git(top, "branch", "--show-current"),
                    "default": head.split("/", 1)[1] if head else None,
                    "dirty": bool(git(top, "status", "--porcelain")),
                }
            repo["instructions"] = [f for f in INSTRUCTION_FILES if os.path.exists(os.path.join(repo.get("root", path), f))]
        records = []
        for p in files:
            meta = frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            records.append({
                "file": p.name,
                "name": meta.get("name", p.stem),
                "type": meta.get("type", ""),
                "description": meta.get("description", ""),
                "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d"),
                "age_days": (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).days,
            })
        out.append({
            "id": pid,
            "memory_dir": str(mdir),
            "path": path,
            "exists": exists,
            "repo": repo,
            "index_bytes": index.stat().st_size if index.exists() else 0,
            "records": records,
            "root": top_root(path) if exists else None,
            "policy": policy_for(path, roots) if exists else None,
            "done": pid in state["done"],
        })
    return out


def cmd_inventory():
    state = load_state()
    print(json.dumps({
        "global_claude": global_claude(),
        "roots": state.get("roots", {}),
        "obsolete": state.get("obsolete", []),
        "projects": inventory(),
    }, ensure_ascii=False, indent=1))


def cmd_status():
    state = load_state()
    if not STATE.exists():
        save_state(state)  # the run's clock starts at the first status
    projects = inventory()
    left = [p for p in projects if not p["done"]]
    total = len(state["done"]) + len(left)
    print(f"Проектов: {total}, разобрано: {len(state['done'])}, осталось: {len(left)}"
          + (f", память выключена {state['finished']}" if state.get("finished") else ""))
    for p in left:
        print(f"  {p['path'] or p['id']}: {len(p['records'])} записей")
    for root, pol in state.get("roots", {}).items():
        print(f"Корень {root}: {pol['owner']}, публикация {pol['publish']}")
    for item in state.get("obsolete", []):
        print(f"Устарело: {item}")
    for repo, b in state.get("branches", {}).items():
        print(f"Ветка {b['branch']} в {repo} → {b['target']}")


OWNERS = ("mine", "team", "company", "public")
PUBLISH = ("direct", "pr", "none")


def cmd_policy(root, owner, publish):
    if owner not in OWNERS or publish not in PUBLISH:
        sys.exit(f"owner: {'|'.join(OWNERS)}; publish: {'|'.join(PUBLISH)}")
    state = load_state()
    state_get(state, "roots", {})[os.path.expanduser(root).rstrip(os.sep)] = {"owner": owner, "publish": publish}
    save_state(state)
    cmd_status()


def cmd_obsolete(text):
    state = load_state()
    state_get(state, "obsolete", []).append(text)
    save_state(state)


def cmd_branch(repo, branch, target):
    state = load_state()
    state_get(state, "branches", {})[repo] = {"branch": branch, "target": target}
    save_state(state)


# $ per MTok: input, output, cache read; cache write is 1.25x input (5 min) or 2x (1 h)
PRICES = [("opus-5", 4, 20, 0.2), ("opus", 5, 25, 0.5), ("sonnet", 3, 15, 0.3), ("haiku", 1, 5, 0.1)]


def price(model):
    return next((p[1:] for p in PRICES if p[0] in (model or "")), PRICES[0][1:])


def cmd_cost():
    """Sum usage of every transcript that ran memtool.py since the run started, plus its subagents."""
    state = load_state()
    started = datetime.fromisoformat(state["started"])
    until = datetime.fromisoformat(state["finished"]) if state.get("finished") else datetime.now()
    since_ts = started.timestamp()
    mains = [t for t in PROJECTS.glob("*/*.jsonl")
             if t.stat().st_mtime >= since_ts and b"memtool.py" in t.read_bytes()]
    files = mains + [s for t in mains for s in t.with_suffix("").glob("subagents/*.jsonl")]
    seen, usd, tokens = set(), 0.0, 0
    for f in files:
        for line in open(f, "rb"):
            if b'"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            msg = row.get("message") or {}
            try:
                ts = datetime.fromisoformat(row.get("timestamp", "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("id") in seen or not since_ts <= ts <= until.timestamp():
                continue
            seen.add(msg.get("id"))
            u = msg.get("usage") or {}
            p_in, p_out, p_read = price(msg.get("model"))
            cc = u.get("cache_creation") or {}
            w1h = cc.get("ephemeral_1h_input_tokens", 0)
            w5m = u.get("cache_creation_input_tokens", 0) - w1h
            usd += (u.get("input_tokens", 0) * p_in + u.get("output_tokens", 0) * p_out
                    + u.get("cache_read_input_tokens", 0) * p_read + w5m * p_in * 1.25 + w1h * p_in * 2) / 1e6
            tokens += u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    minutes = int((until - started).total_seconds() // 60)
    print(f"Разбор: {minutes} мин, {len(seen)} запросов к модели, {tokens / 1e6:.1f} M токенов, ≈ ${usd:.2f} по ценам API "
          f"({len(mains)} сесс., {len(files) - len(mains)} сабагентов)")


def cmd_done(pid):
    state = load_state()
    src = PROJECTS / pid / "memory"
    if src.is_dir():
        dst = ARCHIVE / pid
        if dst.exists():
            dst = ARCHIVE / f"{pid}-{datetime.now():%Y%m%d%H%M%S}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    state["done"][pid] = datetime.now().isoformat(timespec="minutes")
    save_state(state)
    cmd_status()


def cmd_finish():
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    except ValueError:
        sys.exit(f"{SETTINGS} — не JSON, поправь руками: \"autoMemoryEnabled\": false")
    settings["autoMemoryEnabled"] = False
    # write through a symlink (dotfiles) instead of replacing it
    target = SETTINGS.resolve() if SETTINGS.is_symlink() else SETTINGS
    target.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = load_state()
    state["finished"] = datetime.now().isoformat(timespec="minutes")
    save_state(state)
    print(f"autoMemoryEnabled=false в {SETTINGS}. Архив памяти: {ARCHIVE}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["inventory"]:
        cmd_inventory()
    elif args[:1] == ["status"]:
        cmd_status()
    elif args[:1] == ["policy"] and len(args) == 4:
        cmd_policy(*args[1:])
    elif args[:1] == ["obsolete"] and len(args) == 2:
        cmd_obsolete(args[1])
    elif args[:1] == ["branch"] and len(args) == 4:
        cmd_branch(*args[1:])
    elif args[:1] == ["cost"]:
        cmd_cost()
    elif args[:1] == ["done"] and len(args) == 2:
        cmd_done(args[1])
    elif args[:1] == ["finish"]:
        cmd_finish()
    else:
        sys.exit(__doc__)
