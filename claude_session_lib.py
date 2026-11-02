#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude_session_lib — read and migrate Claude Desktop local sessions across accounts.

Layout (macOS):
    ~/Library/Application Support/Claude/claude-code-sessions/
        <accountUuid>/<spaceUuid>/local_<sessionUuid>.json    metadata (per account)
    ~/.claude/projects/<cwd-slug>/<cliSessionId>.jsonl        transcript (shared)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

APP_DIR = Path.home() / "Library/Application Support/Claude"
SESS_ROOT = APP_DIR / "claude-code-sessions"
AGENT_ROOT = APP_DIR / "local-agent-mode-sessions"
PRESENT_ROOT = APP_DIR / "spaces-present"
PROJECTS_ROOT = Path.home() / ".claude/projects"
CONFIG_JSON = APP_DIR / "config.json"
OWNER_JSON = APP_DIR / "cowork-enabled-cli-ops.json"


class MigrateError(Exception):
    pass


# --------------------------------------------------------------------------- basics
def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def claude_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "Claude.app/Contents/MacOS/Claude"],
            capture_output=True, text=True, timeout=5,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def norm_sid(q: str) -> str:
    q = (q or "").strip()
    if q.endswith(".json"):
        q = q[:-5]
    if q.startswith("local_"):
        q = q[len("local_"):]
    return q


def app_ready() -> bool:
    return SESS_ROOT.is_dir()


# --------------------------------------------------------------------------- account / space
def all_accounts() -> list[str]:
    if not SESS_ROOT.is_dir():
        return []
    return sorted(p.name for p in SESS_ROOT.iterdir() if p.is_dir())


def spaces_of(account: str) -> list[str]:
    base = SESS_ROOT / account
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def space_activity(account: str, space: str) -> float:
    """Latest write time inside the space, falling back to the directory mtime."""
    base = SESS_ROOT / account / space
    newest = 0.0
    for f in base.glob("local_*.json"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    if newest == 0.0:
        try:
            newest = base.stat().st_mtime
        except OSError:
            pass
    return newest


def current_account() -> str:
    cfg = read_json(CONFIG_JSON) or {}
    acct = cfg.get("lastKnownAccountUuid")
    if acct and (SESS_ROOT / acct).is_dir():
        return acct
    owner = (read_json(OWNER_JSON) or {}).get("ownerAccountId")
    if owner and (SESS_ROOT / owner).is_dir():
        return owner
    accts = all_accounts()
    if not accts:
        raise MigrateError(f"No account directories found: {SESS_ROOT}")
    return max(accts, key=lambda a: max([space_activity(a, s) for s in spaces_of(a)] or [0]))


def current_space(account: str) -> str:
    spaces = spaces_of(account)
    if not spaces:
        raise MigrateError(f"Account {account} has no space directory. Open the Code/Cowork panel in Claude once first.")
    return max(spaces, key=lambda s: space_activity(account, s))


def is_current(account: str, space: str) -> bool:
    try:
        return account == current_account() and space == current_space(account)
    except MigrateError:
        return False


# --------------------------------------------------------------------------- sessions
def archived_index(account: str, space: str) -> set[str]:
    d = read_json(SESS_ROOT / account / space / "archived-sessions.idx") or {}
    return set(d.get("archived") or [])


def transcript_info(cli_id: str, cwd: str) -> dict:
    if not cli_id:
        return {"found": False, "size": 0, "path": None}
    cands = []
    if cwd and cwd != "-":
        cands.append(PROJECTS_ROOT / cwd.replace("/", "-") / f"{cli_id}.jsonl")
    if PROJECTS_ROOT.is_dir():
        cands.extend(PROJECTS_ROOT.glob(f"*/{cli_id}.jsonl"))
    for c in cands:
        if c.is_file():
            return {"found": True, "size": c.stat().st_size, "path": str(c)}
    return {"found": False, "size": 0, "path": None}


def _public(account: str, space: str, f: Path, data: dict, arch: set[str]) -> dict:
    sid = f.stem
    cli = data.get("cliSessionId")
    cwd = data.get("cwd") or "-"
    turns = data.get("completedTurns")
    return {
        "id": sid,
        "title": data.get("title") or "(untitled)",
        "cwd": cwd,
        "model": data.get("model") or "",
        "effort": data.get("effort") or "",
        "account": account,
        "space": space,
        "cliSessionId": cli,
        "activity": data.get("lastActivityAt") or data.get("createdAt") or 0,
        "created": data.get("createdAt") or 0,
        "turns": turns if isinstance(turns, int) else None,
        "archived": sid in arch,
        "transcript": transcript_info(cli, cwd),
        "file": str(f),
        "current": is_current(account, space),
    }


def iter_sessions() -> list[dict]:
    out = []
    for acct in all_accounts():
        for space in spaces_of(acct):
            arch = archived_index(acct, space)
            for f in sorted((SESS_ROOT / acct / space).glob("local_*.json")):
                data = read_json(f) or {}
                out.append(_public(acct, space, f, data, arch))
    out.sort(key=lambda s: s["activity"] or 0, reverse=True)
    return out


def find_session(q: str) -> list[dict]:
    n = norm_sid(q)
    return [s for s in iter_sessions()
            if s["id"] == f"local_{n}" or s["id"] == q or s["cliSessionId"] == n or s["cliSessionId"] == q]


def get_session(sid: str) -> dict | None:
    for s in iter_sessions():
        if s["id"] == sid:
            return s
    return None


# --------------------------------------------------------------------------- migration
def migrate_one(session: dict, dst_account: str, dst_space: str, *,
                force: bool = False, move: bool = False, dry_run: bool = False) -> dict:
    """Copy one session's metadata into the target account/space."""
    log: list[str] = []
    src_file = Path(session["file"])
    dst_dir = SESS_ROOT / dst_account / dst_space
    dst_file = dst_dir / src_file.name

    if not src_file.is_file():
        return {"ok": False, "log": log, "error": f"Source file missing: {src_file}"}

    if session["account"] == dst_account and session["space"] == dst_space:
        return {"ok": True, "skipped": True, "log": ["Already in that account/space, skipped"], "error": None}

    if dry_run:
        log.append(f"Would write {dst_file}")
        return {"ok": True, "dryRun": True, "log": log, "error": None}

    dst_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    existed = dst_file.exists()
    backup = None
    if existed:
        try:
            same = dst_file.read_bytes() == src_file.read_bytes()
        except OSError:
            same = False
        if same:
            log.append("Identical file already in target, skipped writing")
            existed = False
        elif force:
            backup = dst_file.with_suffix(f".json.bak-{ts}")
            shutil.copy2(dst_file, backup)
            shutil.copy2(src_file, dst_file)
            log.append(f"Backed up and overwrote: {backup.name}")
        else:
            return {"ok": False, "log": log, "error": "Target already has a different file under the same name (use force to overwrite)"}
    else:
        shutil.copy2(src_file, dst_file)
        log.append(f"Wrote {dst_file.name}")

    # a tombstone file hides the session from the list
    tomb = dst_dir / f"deleted_{session['id'][len('local_'):]}"
    if tomb.exists():
        tomb.rename(dst_dir / f"{tomb.name}.bak-{ts}")
        log.append(f"Moved tombstone aside: {tomb.name}")

    if _sync_archive(session, dst_dir):
        log.append("Synced archive state")

    _sync_space_marker(session, dst_account, dst_space, log)

    if move:
        bak = src_file.with_suffix(f".json.moved-{ts}")
        src_file.rename(bak)
        log.append(f"Source renamed to {bak.name}")

    return {
        "ok": True, "log": log, "error": None,
        "dst": str(dst_file),
        "created": not existed,
        "backup": str(backup) if backup else None,
    }


def _sync_archive(session: dict, dst_dir: Path) -> bool:
    src_idx = SESS_ROOT / session["account"] / session["space"] / "archived-sessions.idx"
    want = session["id"] in (read_json(src_idx) or {}).get("archived", [])
    dst_idx = dst_dir / "archived-sessions.idx"
    cur = read_json(dst_idx) or {"v": 1, "archived": []}
    cur.setdefault("archived", [])
    sid = session["id"]
    changed = False
    if want and sid not in cur["archived"]:
        cur["archived"].append(sid)
        changed = True
    if not want and sid in cur["archived"]:
        cur["archived"].remove(sid)
        changed = True
    if changed:
        dst_idx.write_text(json.dumps(cur, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return changed


def _sync_space_marker(session: dict, dst_account: str, dst_space: str, log: list[str]) -> None:
    src = PRESENT_ROOT / session["account"] / session["space"] / "link-key"
    dst = PRESENT_ROOT / dst_account / dst_space / "link-key"
    if src.is_file() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log.append("Added space marker link-key")


# --------------------------------------------------------------------------- snapshot (for the UI)
def snapshot() -> dict:
    acct = current_account()
    space = current_space(acct)
    sessions = iter_sessions()
    accounts = []
    for a in all_accounts():
        accs = [s for s in sessions if s["account"] == a]
        accounts.append({
            "id": a,
            "short": a[:8],
            "isCurrent": a == acct,
            "count": len(accs),
            "spaces": [
                {"id": sp, "short": sp[:8], "count": sum(1 for s in accs if s["space"] == sp),
                 "isCurrent": a == acct and sp == space}
                for sp in spaces_of(a)
            ],
        })
    return {
        "current": {"account": acct, "space": space},
        "accounts": accounts,
        "sessions": sessions,
        "claudeRunning": claude_running(),
        "appDir": str(APP_DIR),
        "projectsRoot": str(PROJECTS_ROOT),
        "journal": [{"id": b["id"], "ts": b["ts"], "count": len(b["items"]),
                     "target": b.get("target"), "undone": b.get("undone", False)}
                    for b in read_journal()[:5]],
    }


# --------------------------------------------------------------------------- session preview
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I)
_NOISE_RE = re.compile(r"<(command-name|command-message|local-command-stdout)>.*?</\1>", re.S | re.I)


def _clean_user_text(text: str) -> str:
    """Strip system-reminder blocks and command echoes injected into user messages."""
    t = _REMINDER_RE.sub("", text or "")
    t = _NOISE_RE.sub("", t)
    return t.strip()


def _blocks_to_text(content) -> str:
    """Keep only meaningful text blocks; tool-only / thinking-only turns yield an empty string."""
    if isinstance(content, str):
        return content.strip()
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            txt = (b.get("text") or "").strip()
            if txt:
                out.append(txt)
    return "\n".join(out)


def session_preview(session: dict, limit: int = 24) -> dict:
    """Extract conversational turns that carry text (skips thinking / tool_use / tool_result)."""
    path = (session.get("transcript") or {}).get("path")
    if not path or not Path(path).is_file():
        return {"messages": [], "firstUser": None, "counts": {"user": 0, "assistant": 0},
                "error": "Transcript jsonl not found"}

    msgs = []
    counts = {"user": 0, "assistant": 0}
    first_user = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"role"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message") or {}
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _blocks_to_text(m.get("content")).strip()
                if role == "user":
                    text = _clean_user_text(text)
                if not text:
                    continue
                # the same turn can be written twice (prompt + injected block); drop neighbours
                if msgs and msgs[-1]["role"] == role and msgs[-1]["raw"] == text:
                    continue
                counts[role] += 1
                if role == "user" and first_user is None:
                    first_user = text[:400]
                msgs.append({
                    "role": role,
                    "raw": text,
                    "text": text if len(text) <= 2000 else text[:2000] + "…",
                    "ts": d.get("timestamp"),
                })
    except Exception as e:
        return {"messages": [], "firstUser": None, "counts": counts, "error": f"{type(e).__name__}: {e}"}

    return {
        "messages": [{k: v for k, v in m.items() if k != "raw"} for m in msgs[-limit:]],
        "firstUser": first_user,
        "counts": counts,
        "truncated": len(msgs) > limit,
        "total": len(msgs),
    }


# --------------------------------------------------------------------------- journal / undo
JOURNAL_PATH = Path.home() / ".claude" / "session-migrate-journal.json"
JOURNAL_MAX = 40


def read_journal() -> list[dict]:
    d = read_json(JOURNAL_PATH)
    return d if isinstance(d, list) else []


def write_journal(batches: list[dict]) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_PATH.write_text(
        json.dumps(batches[:JOURNAL_MAX], ensure_ascii=False, indent=1), encoding="utf-8")


def add_batch(target: dict, items: list[dict]) -> dict:
    batch = {
        "id": f"b{int(time.time() * 1000)}",
        "ts": int(time.time() * 1000),
        "target": target,
        "items": items,
        "undone": False,
    }
    batches = read_journal()
    batches.insert(0, batch)
    write_journal(batches)
    return batch


def undo_batch(batch_id: str) -> dict:
    batches = read_journal()
    batch = next((b for b in batches if b["id"] == batch_id), None)
    if not batch:
        return {"ok": False, "error": "No such migration batch"}
    if batch.get("undone"):
        return {"ok": False, "error": "This batch has already been undone"}

    ts = time.strftime("%Y%m%d-%H%M%S")
    log, failed = [], 0
    for it in batch["items"]:
        dst = Path(it["dst"])
        try:
            if it.get("backup") and Path(it["backup"]).is_file():
                shutil.copy2(it["backup"], dst)
                log.append(f"Restored overwritten file: {dst.name}")
            elif dst.is_file():
                dst.rename(dst.with_suffix(f".json.undone-{ts}"))
                log.append(f"Withdrew from target: {dst.name}")
            else:
                log.append(f"Target no longer exists, skipped: {dst.name}")
        except OSError as e:
            failed += 1
            log.append(f"Undo failed for {dst.name}: {e}")

    batch["undone"] = True
    batch["undoneAt"] = int(time.time() * 1000)
    write_journal(batches)
    return {"ok": True, "failed": failed, "log": log}

