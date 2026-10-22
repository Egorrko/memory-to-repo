#!/usr/bin/env python3
"""Helper for the memory-to-repo skill. Stdlib only; Linux, macOS, Windows.

  memtool.py inventory          JSON: every project with auto-memory, its real path, repo facts, records
  memtool.py status             progress: which projects are done, step k/N
  memtool.py done <project-id>  move that project's memory dir to the archive, mark it done
  memtool.py finish             autoMemoryEnabled=false in ~/.claude/settings.json, mark the run finished

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
        return {"started": datetime.now().isoformat(timespec="minutes"), "done": {}, "finished": None}


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
            })
        out.append({
            "id": pid,
            "memory_dir": str(mdir),
            "path": path,
            "exists": exists,
            "repo": repo,
            "index_bytes": index.stat().st_size if index.exists() else 0,
            "records": records,
            "done": pid in state["done"],
        })
    return out


def cmd_inventory():
    print(json.dumps(inventory(), ensure_ascii=False, indent=1))


def cmd_status():
    state = load_state()
    projects = inventory()
    left = [p for p in projects if not p["done"]]
    total = len(state["done"]) + len(left)
    print(f"Проектов: {total}, разобрано: {len(state['done'])}, осталось: {len(left)}"
          + (f", память выключена {state['finished']}" if state.get("finished") else ""))
    for p in left:
        print(f"  {p['path'] or p['id']}: {len(p['records'])} записей")


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
    elif args[:1] == ["done"] and len(args) == 2:
        cmd_done(args[1])
    elif args[:1] == ["finish"]:
        cmd_finish()
    else:
        sys.exit(__doc__)
