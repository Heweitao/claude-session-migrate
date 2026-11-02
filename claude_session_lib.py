#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude_session_lib — read and migrate Claude Desktop local sessions across accounts.

Layout (macOS). A profile is a data directory; Chromium's --user-data-dir selects it,
so each profile keeps its own login. The default profile is the app's normal folder,
extra ones live under ~/.claude-accounts/:

    <app dir>/
    └── claude-code-sessions/
        └── <accountUuid>/<spaceUuid>/
            ├── local_<sessionUuid>.json    metadata (per account + profile)
            ├── deleted_<sessionUuid>        tombstone
            └── archived-sessions.idx
    ~/.claude/projects/<cwd-slug>/<cliSessionId>.jsonl   transcript (shared, global)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_PROFILE = "default"
DEFAULT_APP_DIR = Path.home() / "Library/Application Support/Claude"
PROFILES_ROOT = Path.home() / ".claude-accounts"

PROJECTS_ROOT = Path.home() / ".claude/projects"
JOURNAL_PATH = Path.home() / ".claude" / "session-migrate-journal.json"
JOURNAL_MAX = 40


class MigrateError(Exception):
    pass


# --------------------------------------------------------------------------- profiles
def app_dir(profile: str) -> Path:
    if not profile or profile == DEFAULT_PROFILE:
        return DEFAULT_APP_DIR
    return PROFILES_ROOT / profile


def discover_profiles() -> list[str]:
    """Default profile first, then any directory under ~/.claude-accounts."""
    names = []
    if DEFAULT_APP_DIR.is_dir():
        names.append(DEFAULT_PROFILE)
    if PROFILES_ROOT.is_dir():
        for p in sorted(PROFILES_ROOT.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                names.append(p.name)
    return names


def require_profiles() -> list[str]:
    names = discover_profiles()
    if not names:
        raise MigrateError(f"No Claude data directory found (looked in {DEFAULT_APP_DIR})")
    return names


def sess_root(profile: str) -> Path:
    return app_dir(profile) / "claude-code-sessions"


def present_root(profile: str) -> Path:
    return app_dir(profile) / "spaces-present"


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
    return bool(discover_profiles())


# --------------------------------------------------------------------------- accounts / spaces
def all_accounts(profile: str) -> list[str]:
    base = sess_root(profile)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def spaces_of(profile: str, account: str) -> list[str]:
    base = sess_root(profile) / account
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def space_activity(profile: str, account: str, space: str) -> float:
    """Latest write time inside the space, falling back to the directory mtime."""
    base = sess_root(profile) / account / space
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


def current_account(profile: str) -> str:
    cfg = read_json(app_dir(profile) / "config.json") or {}
    acct = cfg.get("lastKnownAccountUuid")
    if acct and (sess_root(profile) / acct).is_dir():
        return acct
    owner = (read_json(app_dir(profile) / "cowork-enabled-cli-ops.json") or {}).get("ownerAccountId")
    if owner and (sess_root(profile) / owner).is_dir():
        return owner
    accts = all_accounts(profile)
    if not accts:
        raise MigrateError(f"Profile '{profile}' has no account directories yet.")
    return max(accts, key=lambda a: max([space_activity(profile, a, s) for s in spaces_of(profile, a)] or [0]))


def current_space(profile: str, account: str) -> str:
    spaces = spaces_of(profile, account)
    if not spaces:
        raise MigrateError(
            f"Account {account} in profile '{profile}' has no space directory. "
            "Open the Code/Cowork panel in Claude once.")
    return max(spaces, key=lambda s: space_activity(profile, account, s))


def is_current(profile: str, account: str, space: str) -> bool:
    try:
        return account == current_account(profile) and space == current_space(profile, account)
    except MigrateError:
        return False


# --------------------------------------------------------------------------- sessions
def archived_index(profile: str, account: str, space: str) -> set[str]:
    d = read_json(sess_root(profile) / account / space / "archived-sessions.idx") or {}
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


def _public(profile: str, account: str, space: str, f: Path, data: dict, arch: set[str]) -> dict:
    sid = f.stem
    cli = data.get("cliSessionId")
    cwd = data.get("cwd") or "-"
    turns = data.get("completedTurns")
    return {
        "id": sid,
        "profile": profile,
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
        "current": is_current(profile, account, space),
    }


def iter_sessions() -> list[dict]:
    out = []
    for profile in require_profiles():
        for acct in all_accounts(profile):
            for space in spaces_of(profile, acct):
                arch = archived_index(profile, acct, space)
                for f in sorted((sess_root(profile) / acct / space).glob("local_*.json")):
                    data = read_json(f) or {}
                    out.append(_public(profile, acct, space, f, data, arch))
    out.sort(key=lambda s: s["activity"] or 0, reverse=True)
    return out


def find_session(q: str) -> list[dict]:
    n = norm_sid(q)
    return [s for s in iter_sessions()
            if s["id"] == f"local_{n}" or s["id"] == q or s["cliSessionId"] == n or s["cliSessionId"] == q]


def get_session(sid: str, profile: str | None = None) -> dict | None:
    for s in iter_sessions():
        if s["id"] == sid and (profile is None or s["profile"] == profile):
            return s
    return None


# --------------------------------------------------------------------------- migration
def migrate_one(session: dict, dst_profile: str, dst_account: str, dst_space: str, *,
                force: bool = False, move: bool = False, dry_run: bool = False) -> dict:
    """Copy one session's metadata into the target profile/account/space."""
    log: list[str] = []
    src_file = Path(session["file"])
    src_profile, src_account, src_space = session["profile"], session["account"], session["space"]
    dst_dir = sess_root(dst_profile) / dst_account / dst_space
    dst_file = dst_dir / src_file.name

    if not src_file.is_file():
        return {"ok": False, "log": log, "error": f"Source file missing: {src_file}"}

    if (src_profile, src_account, src_space) == (dst_profile, dst_account, dst_space):
        return {"ok": True, "skipped": True,
                "log": ["Already in that profile/account/space, skipped"], "error": None}

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
            return {"ok": False, "log": log,
                    "error": "Target already has a different file under the same name (use force to overwrite)"}
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

    _sync_space_marker(session, dst_profile, dst_account, dst_space, log)

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
    src_idx = sess_root(session["profile"]) / session["account"] / session["space"] / "archived-sessions.idx"
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


def _sync_space_marker(session: dict, dst_profile: str, dst_account: str, dst_space: str,
                       log: list[str]) -> None:
    src = present_root(session["profile"]) / session["account"] / session["space"] / "link-key"
    dst = present_root(dst_profile) / dst_account / dst_space / "link-key"
    if src.is_file() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log.append("Added space marker link-key")


# --------------------------------------------------------------------------- snapshot (for the UI)
def snapshot() -> dict:
    sessions = iter_sessions()
    profiles = []
    for name in require_profiles():
        accts = [s for s in sessions if s["profile"] == name]
        try:
            cur_acct = current_account(name)
            cur_space = current_space(name, cur_acct)
        except MigrateError:
            cur_acct, cur_space = None, None
        accounts = []
        for a in all_accounts(name):
            accs = [s for s in accts if s["account"] == a]
            accounts.append({
                "id": a,
                "short": a[:8],
                "isCurrent": a == cur_acct,
                "count": len(accs),
                "spaces": [
                    {"id": sp, "short": sp[:8],
                     "count": sum(1 for s in accs if s["space"] == sp),
                     "isCurrent": a == cur_acct and sp == cur_space}
                    for sp in spaces_of(name, a)
                ],
            })
        profiles.append({
            "name": name,
            "path": str(app_dir(name)),
            "isDefault": name == DEFAULT_PROFILE,
            "current": {"account": cur_acct, "space": cur_space},
            "accounts": accounts,
            "count": len(accts),
        })
    return {
        "profiles": profiles,
        "currentProfile": next((p["name"] for p in profiles if p["isDefault"]),
                               profiles[0]["name"] if profiles else None),
        "sessions": sessions,
        "claudeRunning": claude_running(),
        "profilesRoot": str(PROFILES_ROOT),
        "journal": [{"id": b["id"], "ts": b["ts"], "count": len(b["items"]),
                     "target": b.get("target"), "undone": b.get("undone", False)}
                    for b in read_journal()[:5]],
    }


# --------------------------------------------------------------------------- preview
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
