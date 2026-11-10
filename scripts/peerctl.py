#!/usr/bin/env python3
#
# VENDORED COPY — install source for another host, not the authoritative one.
#
# The live copy on the machine this architecture came from is
# ~/.hermes/scripts/peerctl.py; that file is what its own hooks run, and it is the one
# edited when the mechanism changes. This copy exists so a second host can install the
# guard by cloning one public repo instead of being sent a file through chat.
#
# Source sha256: facda52b3e715cc01dd651b8a5fe57c15c97012231cbed8b0be71265d95c0189
# Vendored: 2026-11-11
#
# Install: copy to ~/.hermes/scripts/peerctl.py, wire the four hooks exactly as
# config-hooks.yaml in this directory shows, and restart the agent. Design and its
# defects: design/implementations/elio-2026-11-09-concurrent-sessions.md in the log
# repo (willhadfield/agent-memory). Re-copy, do not hand-merge, when the source changes.
#
"""peerctl — awareness and coordination between concurrent Hermes sessions on this host.

Every Hermes session (desktop chat, CLI, cron job, subagent parent) shares one
filesystem, so two sessions can quietly clobber each other's work. Hermes has no
cross-session awareness of its own: its locks protect state.db, not your repos.

This is the missing piece. One JSON file per live session under
``~/.hermes/data/peers/sessions/``, written only by that session (so no locking),
plus a per-session inbox for messages between sessions. Shell hooks
(``hooks:`` in config.yaml) call ``peerctl.py hook`` on every session start, turn
and mutating tool call, so:

  * every session registers itself and beats a heartbeat automatically;
  * a session's goal is captured from its first user message, no model effort;
  * writing a file auto-claims its repo (or its directory), and another live
    session writing into that claim is BLOCKED with a message naming the peer;
  * at the start of every turn a session is told which peers are live, what they
    are working on, and anything they have said to it.

Design rules that matter:
  * stdlib only, /usr/bin/python3 — hooks run as subprocesses and must start fast;
  * fail open, always exit 0: ``pre_tool_call`` fails closed on a crash, so a bug
    here would wedge every tool call in every session. Nothing may raise;
  * per-session file = single writer, so no locks and no torn reads;
  * a claim lapses when its session goes quiet (45 min), because a stale claim
    that blocks forever is worse than a collision;
  * the escape hatch is explicit and recorded: ``claim <path> --shared``.

CLI:
  peerctl list                      live sessions, goals, claims, unread
  peerctl show <session>            one session in full
  peerctl goal "refactor foo"       set this session's goal by hand
  peerctl claim <path> [--why ...] [--shared]
  peerctl release <path>
  peerctl say <session|--all> TEXT  message another session (next turn)
  peerctl inbox [--peek]            read this session's inbox by hand
  peerctl gc                        prune dead session files
  peerctl status                    one-line summary for scripts

  peerctl work start <slug> --outcome "what will be different" [--scope PATH]
  peerctl work update <slug> --state planned|doing|waiting [--note ...]
  peerctl work offer <slug> --to zeta        # sender stays accountable until accepted
  peerctl work accept <slug>                 # the accepting session becomes accountable
  peerctl work finish <slug> --evidence "path or revision"     # no evidence -> unknown
  peerctl work list | sheet [--json]         # one row per slug, owner resolved

Session id resolution for the CLI: $HERMES_SESSION_ID, else --session, else the
most recently active session whose file is fresh (that is almost always you).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
PEERS = HOME / ".hermes" / "data" / "peers"
SESSIONS = PEERS / "sessions"
INBOX = PEERS / "inbox"
LOG = PEERS / "events.jsonl"

STALE_S = 45 * 60          # a session with no heartbeat for this long is gone
MAX_CLAIMS = 40            # per session; oldest fall off
CONTEXT_MAX = 1900         # characters injected into a turn; fitted by priority
GOAL_MAX = 160
INBOX_KEEP = 2000          # lines; older messages are dropped on append

# Tools whose calls can change files on disk. Everything else just heartbeats.
MUTATING_TOOLS = {"write_file", "patch"}

# A terminal command that writes into somebody else's claim is the other half of
# the clobber risk. Read-only commands in a shared repo stay allowed: `git status`
# and `ls` are most of what a second session should be doing there anyway.
_MUTATING_CMD = re.compile(
    r"(?:^|[;&|(]\s*|\bsudo\s+)(?:"
    r"git\s+(?:-[Cc]\s+\S+\s+|--?[A-Za-z][\w-]*\s+)*\s*"
    r"(?:add|commit|push|pull|merge|rebase|reset|checkout|switch|restore|"
    r"stash|clean|cherry-pick|apply|tag|rm|mv)\b"
    r"|rm\b|mv\b|cp\b|sed\s+-i|truncate\b|systemctl\s+(?:start|stop|restart|enable|disable)\b"
    r"|(?:pnpm|npm|yarn)\s+(?:add|install|remove|ci)\b|pip3?\s+install\b|uv\s+(?:add|sync)\b"
    r")"
)
_ABS_PATH = re.compile(r"(?:^|[\s='\"])((?:~|/)[\w./@+-]+)")


# --------------------------------------------------------------------------- io


def _now() -> float:
    return time.time()


def _safe(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(sid))[:120] or "unknown"


def _session_file(sid: str) -> Path:
    return SESSIONS / (_safe(sid) + ".json")


def _inbox_file(sid: str) -> Path:
    return INBOX / (_safe(sid) + ".jsonl")


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _append_line(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)  # single small write under O_APPEND: atomic on Linux
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _log(event: str, **fields) -> None:
    try:
        _append_line(LOG, {"ts": _now(), "event": event, **fields})
    except OSError:
        pass


# ---------------------------------------------------------------------- records


def load(sid: str) -> dict:
    return _read_json(_session_file(sid))


def save(rec: dict) -> None:
    _atomic_json(_session_file(rec["session_id"]), rec)


def all_records() -> list[dict]:
    out = []
    try:
        files = list(SESSIONS.glob("*.json"))
    except OSError:
        return out
    for f in files:
        rec = _read_json(f)
        if rec.get("session_id"):
            out.append(rec)
    return out


def is_live(rec: dict, now: float | None = None) -> bool:
    beat = rec.get("heartbeat") or 0
    return (now or _now()) - float(beat) < STALE_S


def live_records(exclude: str | None = None) -> list[dict]:
    now = _now()
    recs = [r for r in all_records() if is_live(r, now) and r["session_id"] != exclude]
    recs.sort(key=lambda r: -(r.get("heartbeat") or 0))
    return recs


def _clean_goal(text: str) -> tuple[str, bool]:
    """Turn the first user message into something a peer can read.

    Returns (goal, was_wrapper). The message a session is handed is often not what the
    person asked: cron jobs get their prompt, but a skill-invoked turn gets the whole
    skill directive, and the recall hook's matches are injected above the question. On
    9 Nov 2026 a live peer's goal was literally ``[IMPORTANT: The user has invoked the
    "github" skill, …] --- name: git`` — awareness that existed and told a reader nothing.
    A wrapper is not a goal, and truncating it at 160 characters makes every such peer
    look identical, which is worse than an empty field.
    """
    raw = " ".join((text or "").split())
    if not raw:
        return "", False
    t = raw
    wrapped = False
    for _ in range(3):  # wrappers can nest: [IMPORTANT: … [System: …] …]
        stripped = re.sub(r"^\[(IMPORTANT|System|OUT-OF-BAND)[^\]]*\]\s*", "", t, flags=re.I)
        if stripped == t:
            break
        t, wrapped = stripped, True
    m = re.match(r'^The user has invoked the "?([A-Za-z0-9:_-]+)"? skill', t, re.I)
    if m:
        return f"skill: {m.group(1)}", True
    t = re.sub(r"^RECALL\b.*?(?=\s[A-Z0-9]|\Z)", "", t, flags=re.S).strip()
    if t.startswith(("---", "name:")):
        m2 = re.search(r"name:\s*([A-Za-z0-9_.-]+)", t)
        return (f"skill: {m2.group(1)}" if m2 else "skill directive"), True
    # A question buried under injected context still ends in a question mark; prefer that.
    m3 = re.search(r"([A-Z][^.!?]{8,150}\?)", t)
    if wrapped and m3:
        return m3.group(1).strip(), True
    return (t or raw), wrapped


def _title(rec: dict) -> str:
    goal = (rec.get("goal") or "").strip().replace("\n", " ")
    if not goal:
        goal = "(no stated goal)"
    return goal if len(goal) <= 72 else goal[:69] + "…"


def touch(sid: str, *, surface: str = "", model: str = "", platform: str = "",
          cwd: str = "", goal: str | None = None) -> dict:
    """Register or heartbeat this session. Returns the fresh record."""
    rec = load(sid)
    now = _now()
    if not rec:
        rec = {"session_id": sid, "started_at": now, "claims": {}, "inbox_read": 0,
               "turns": 0, "tools": 0, "goal_source": "auto"}
        first = True
    else:
        first = False
    rec["heartbeat"] = now
    rec["pid"] = os.getppid()
    if surface:
        rec["surface"] = surface
    if model:
        rec["model"] = model
    if platform:
        rec["platform"] = platform
    if cwd:
        rec["cwd"] = cwd
    if goal:
        cleaned, was_wrapper = _clean_goal(goal)
        existing = (rec.get("goal") or "").strip()
        take = first or not existing or rec.get("goal_source") == "auto"
        # A later real request beats an earlier wrapper; a wrapper never overwrites a goal
        # that reads like a task someone actually asked for.
        keep_existing = was_wrapper and existing and not existing.startswith(("skill:", "[", "("))
        if cleaned and take and not keep_existing:
            rec["goal"] = cleaned[:GOAL_MAX]
            rec["goal_source"] = "auto"
    save(rec)
    return rec


# ----------------------------------------------------------------------- claims


def _resolve(path: str) -> str:
    try:
        return str(Path(os.path.expanduser(str(path))).resolve())
    except (OSError, RuntimeError):
        return ""


def repo_root(path: str) -> str:
    """The git repo containing `path`, or "" when it is not in one."""
    p = Path(path)
    start = p if p.is_dir() else p.parent
    for cand in [start, *start.parents]:
        if (cand / ".git").exists():
            return str(cand)
        if str(cand) == str(HOME):
            break
    return ""


def _ignored(path: str) -> bool:
    """Our own bookkeeping, per-session scratch and temp files never collide."""
    if not path:
        return True
    p = os.path.realpath(path)
    for skip in (str(PEERS), str(HOME / ".hermes" / "cache"), "/tmp", "/proc",
                 "/var/tmp", str(HOME / ".hermes" / "logs")):
        if p == skip or p.startswith(skip + os.sep):
            return True
    return False


def _under(path: str, root: str) -> bool:
    return bool(path) and bool(root) and (path == root or path.startswith(root + os.sep))


def _overlaps(target: str, scope: str, claim: dict) -> bool:
    """Does this claim cover the thing about to be touched?

    File claims collide only with the same file (two sessions editing different
    files in one repo is normal and must keep working — a memory or notes repo
    would otherwise be unusable). A repo claim — taken by a mutating git command
    or by an explicit manual claim on a directory — covers every file inside it,
    and a repo-scope check (a git command) is blocked by any file claim inside
    that repo, because `git add -A` would sweep their half-written work.
    """
    cpath, cscope = claim.get("path", ""), claim.get("scope", "file")
    if not target or not cpath:
        return False
    if cscope == "repo":
        return _under(target, cpath) or _under(cpath, target)
    if scope == "repo":
        return _under(cpath, target)
    return target == cpath


def collisions(sid: str, path: str, scope: str = "file") -> list[tuple[dict, dict]]:
    """Other live sessions whose unshared claim covers what is about to be touched.

    A shared claim held by the caller on the overlapping path means the caller has
    already recorded the decision to work jointly, so it is not blocked again.
    """
    key = _resolve(path)
    if not key:
        return []
    if scope == "repo":
        key = repo_root(key) or key
    if _ignored(key):
        return []
    mine = load(sid).get("claims") or {}
    for my_claim in mine.values():
        if my_claim.get("shared") and _overlaps(key, scope, my_claim):
            return []
    out = []
    for rec in live_records(exclude=sid):
        for claim in (rec.get("claims") or {}).values():
            if claim.get("shared"):
                continue
            if _overlaps(key, scope, claim):
                out.append((rec, claim))
    return out


def add_claim(rec: dict, path: str, *, scope: str = "file", kind: str = "auto",
              shared: bool = False, why: str = "") -> str:
    key = _resolve(path)
    if not key or _ignored(key):
        return ""
    if scope == "repo":
        key = repo_root(key) or key
    ckey = f"{scope}:{key}"
    claims = rec.setdefault("claims", {})
    existing = claims.get(ckey)
    if existing:
        existing["seen"] = _now()
        if shared:
            existing["shared"] = True
        if why:
            existing["why"] = why
        return ckey
    claims[ckey] = {"path": key, "scope": scope, "kind": kind, "shared": shared,
                    "since": _now(), "seen": _now(), "why": why}
    if len(claims) > MAX_CLAIMS:
        oldest = sorted(claims, key=lambda k: claims[k].get("seen") or 0)
        for k in oldest[: len(claims) - MAX_CLAIMS]:
            claims.pop(k, None)
    return ckey


def release_claim(rec: dict, path: str) -> bool:
    key = _resolve(path)
    claims = rec.get("claims") or {}
    released = False
    for ck in list(claims):
        scope, _, cpath = ck.partition(":")
        if cpath == key or _overlaps(key, "file", {"path": cpath, "scope": scope}):
            claims.pop(ck, None)
            released = True
    return released


def _short(path: str) -> str:
    return "~" + path[len(str(HOME)):] if path.startswith(str(HOME)) else path


def _describe_claims(rec: dict, limit: int = 5) -> str:
    """Compact claim summary, grouped by repo so ten edited files read as one entry."""
    claims = list((rec.get("claims") or {}).values())
    if not claims:
        return ""
    groups: dict[str, list[dict]] = {}
    for c in claims:
        base = repo_root(c.get("path", "")) or c.get("path", "")
        groups.setdefault(base, []).append(c)
    out = []
    for base, items in list(groups.items())[:limit]:
        files = [c for c in items if c.get("scope") != "repo"]
        repos = [c for c in items if c.get("scope") == "repo"]
        tag = []
        if any(c.get("shared") for c in items):
            tag.append("shared")
        if repos:
            tag.append("whole repo" + (" · git" if repos[0].get("kind") == "git" else ""))
        elif files:
            tag.append(f"{len(files)} files" if len(files) > 1 else "1 file")
        out.append(f"{_short(base)} ({', '.join(tag)})")
    if len(groups) > limit:
        out.append(f"+{len(groups) - limit} more")
    return ", ".join(out)


# ------------------------------------------------------------------------ inbox


def say(target: str, text: str, *, sender: str, sender_title: str = "") -> int:
    """Deliver a message to another session's next turn."""
    if target == "all":
        rec = load(sender)
        targets = [r["session_id"] for r in live_records(exclude=sender)]
    else:
        rec = load(sender)
        targets = [target]
    for t in targets:
        ib = _inbox_file(t)
        try:
            if ib.exists() and ib.stat().st_size > 600_000:
                ib.unlink()  # pathological growth; a session that never reads
        except OSError:
            pass
        _append_line(ib, {"ts": _now(), "from": sender, "from_title": sender_title,
                          "text": " ".join(str(text).split())})
    _log("say", sender=sender, targets=targets, chars=len(text))
    return len(targets)


def unread(rec: dict) -> list[dict]:
    ib = _inbox_file(rec["session_id"])
    if not ib.exists():
        return []
    try:
        lines = ib.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    start = int(rec.get("inbox_read") or 0)
    msgs = []
    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            msgs.append(obj)
    rec["inbox_read"] = len(lines)
    return msgs


# --------------------------------------------------------------------- context


def peer_context(sid: str, *, budget: int = CONTEXT_MAX) -> str:
    """The block injected at the start of a turn. Empty when there is nothing to say.

    Parts carry a priority and are fitted whole: messages outrank the peer list, which
    outranks the work sheet. Truncating the assembled string cut whichever section
    happened to be last, so a tight budget silently dropped a peer's *message* before it
    dropped a row of work history. A half-shown section is worse than an absent one.
    """
    parts: list[dict] = []          # {"prio": int, "text": str}; lower prio wins the budget
    peers = [r for r in live_records(exclude=sid) if r.get("heartbeat")]
    if peers:
        lines = [f"[peers] {len(peers)} other Hermes session(s) are live on this host:"]
        for r in peers[:6]:
            bits = [f"- {r['session_id']} · {_title(r)}"]
            if r.get("surface"):
                bits[0] += f" · {r['surface']}"
            claims = _describe_claims(r)
            if claims:
                bits.append("working in " + claims)
            age = int((_now() - float(r.get("heartbeat") or 0)) / 60)
            bits.append(f"active {age}m ago")
            lines.append("  ".join(bits))
        if len(peers) > 6:
            lines.append(f"  … and {len(peers) - 6} more (`peerctl list`)")
        lines.append(
            "The files and whole-repo claims above are off limits: writing one, or running a "
            "mutating git command in a repo another session is editing, is blocked. A different "
            "file in the same repo is fine. Coordinate with `peerctl say <session> \"…\"`, or "
            "claim joint ownership with `peerctl claim <path> --shared --why \"…\"`."
        )
        parts.append({"prio": 1, "text": "\n".join(lines)})
    # What work exists — the thing a session needs in order not to repeat a peer's job or
    # take over a dead one's. Kept to four active and three recently closed, because a
    # record nobody can scan in one look is a record nobody consults.
    try:
        rows = work_effective()
    except Exception:  # noqa: BLE001 - awareness is never worth a failed turn
        rows = []
    if rows:
        now = _now()
        active = [r for r in rows if r.get("state") in ("planned", "doing", "waiting", "offered")]
        fresh = [r for r in rows if r.get("state") in ("finished", "failed", "abandoned")
                 and (now - float(r.get("updated") or 0)) < FINISHED_VISIBLE]
        lines = ["[work] shared records — check here before starting anything a peer may have:"]
        for r in active[:4]:
            owner = r.get("owner_effective")
            who = "this session" if owner == sid else (owner or "?")
            line = f"- {r['slug']}: {r.get('state')}, {who}, {_ago(float(r.get('updated') or 0))} ago"
            if r.get("outcome"):
                line += f" — {str(r['outcome'])[:90]}"
            lines.append(line)
            if owner != sid and r.get("state") in ("doing", "planned", "waiting"):
                sess = load(str(owner))
                if not sess or not is_live(sess, now):
                    lines.append("  owner unconfirmed: check the files before taking this over")
        for r in fresh[:3]:
            lines.append(f"- {r['slug']}: {r.get('state')} {_ago(float(r.get('updated') or 0))} ago"
                         + (f" — evidence {str(r['evidence'])[:70]}" if r.get("evidence") else "")
                         + ". Do not redo it.")
        lines.append("Record your own with `peerctl work start <slug> --outcome \"…\"`; close it "
                     "with `--evidence` so the next session does not repeat it.")
        parts.append({"prio": 2, "text": "\n".join(lines)})
    rec = load(sid)
    msgs = unread(rec) if rec else []
    if msgs:
        save(rec)
        lines = ["[peers] message(s) from other sessions:"]
        for m in msgs[-5:]:
            who = m.get("from", "?")
            title = (m.get("from_title") or "").strip()
            head = f"- from {who}" + (f" ({title})" if title else "")
            lines.append(f"{head}: {m.get('text', '')[:400]}")
        lines.append("Reply with `peerctl say <session> \"…\"` when a response is owed.")
        parts.append({"prio": 0, "text": "\n".join(lines)})
    # Fit by priority, render in the order written: a section either appears whole or not
    # at all, and a message is never the thing that gets cut.
    keep: set[int] = set()
    total = 0
    for idx in sorted(range(len(parts)), key=lambda i: parts[i]["prio"]):
        extra = len(parts[idx]["text"]) + (2 if keep else 0)
        if total + extra <= budget:
            keep.add(idx)
            total += extra
    return "\n\n".join(parts[i]["text"] for i in range(len(parts)) if i in keep)


# ----------------------------------------------------------------------- hooks


def _hook_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _first_user_text(extra: dict) -> str:
    um = extra.get("user_message")
    if isinstance(um, str):
        return um
    if isinstance(um, list):  # multimodal turn: content parts
        for part in um:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text") or "")
    return ""


def hook_main(argv: list[str]) -> int:
    if os.environ.get("HERMES_PEERS_DISABLE"):
        return 0
    payload = _hook_payload()
    if (PEERS / "debug").exists():
        try:
            _append_line(PEERS / "debug.jsonl", {"ts": _now(), "payload": payload})
        except Exception:  # health: allow BLE001 -- debugging aid only
            pass
    event = (payload.get("hook_event_name") or (argv[0] if argv else "")).strip()
    sid = str(payload.get("session_id") or "").strip()
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    if not sid:
        return 0

    if event == "on_session_start":
        try:
            cmd_gc(quiet=True)
        except Exception:  # health: allow BLE001 -- housekeeping must never break a session start
            pass

    if event in ("on_session_start", "pre_llm_call", "pre_tool_call"):
        goal = ""
        if event == "pre_llm_call" and (extra.get("is_first_turn") or not load(sid).get("goal")):
            goal = _first_user_text(extra)
        rec = touch(sid, surface=str(extra.get("platform") or ""),
                    model=str(extra.get("model") or ""),
                    cwd=str(payload.get("cwd") or ""), goal=goal)
        if event == "pre_llm_call":
            rec["turns"] = int(rec.get("turns") or 0) + 1
            save(rec)
            ctx = peer_context(sid)
            if ctx:
                print(json.dumps({"context": ctx}))
        elif event == "pre_tool_call":
            rec["tools"] = int(rec.get("tools") or 0) + 1
            save(rec)
            decision = _check_tool(rec, payload)
            if decision:
                print(json.dumps(decision))
        return 0

    if event in ("on_session_finalize", "on_session_end"):
        # on_session_end fires after EVERY turn — a session is not gone there.
        if event == "on_session_finalize":
            try:
                _session_file(sid).unlink()
                _log("finalize", session=sid)
            except OSError:
                pass
        return 0
    return 0


def _check_tool(rec: dict, payload: dict) -> dict | None:
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    sid = rec["session_id"]

    if tool in MUTATING_TOOLS:
        target = str(args.get("path") or args.get("file_path") or "")
        if not target:
            return None
        target = _resolve(target)
        if not target:
            return None
        hits = collisions(sid, target, "file")
        if hits:
            return {"decision": "block", "reason": _block_message(sid, target, hits)}
        add_claim(rec, target, scope="file")
        _atomic_json(_session_file(sid), rec)
        return None

    if tool == "terminal":
        cmd = str(args.get("command") or "")
        if not _MUTATING_CMD.search(cmd):
            return None
        candidates: list[str] = []
        wd = args.get("workdir")
        if isinstance(wd, str) and wd.strip():
            candidates.append(wd.strip())
        for m in _ABS_PATH.finditer(cmd):
            tok = m.group(1)
            if tok.startswith(("~", "/")) and len(tok) > 1:
                candidates.append(tok)
        if not candidates:
            candidates.append(str(payload.get("cwd") or ""))
        for cand in candidates:
            cand = _resolve(cand)
            if not cand:
                continue
            hits = collisions(sid, cand, "repo")
            if hits:
                return {"decision": "block", "reason": _block_message(sid, cand, hits, command=True)}
        return None

    return None


def _block_message(sid: str, path: str, hits: list[tuple[dict, dict]], *, command: bool = False) -> str:
    lines = []
    shown = _short(path)
    if command:
        lines.append(f"Blocked: this could modify {shown}, which another live Hermes session is working in.")
    else:
        lines.append(f"Blocked: {shown} is being worked on by another live Hermes session.")
    for peer, claim in hits[:3]:
        cp = _short(claim.get("path", ""))
        scope = "whole repo" if claim.get("scope") == "repo" else "file"
        why = f" — {claim['why']}" if claim.get("why") else ""
        lines.append(f"  · session {peer['session_id']} ({_title(peer)}) claims {cp} [{scope}]{why}")
    first = hits[0][0]["session_id"]
    target = (repo_root(path) or path) if command else hits[0][1].get("path", "")
    cp_first = _short(target)
    lines.append("Do not retry blindly. Pick one:")
    lines.append(f"  1. `peerctl say {first} \"…\"` — coordinate, then wait for their reply.")
    lines.append(f"  2. `peerctl claim {cp_first} --shared --why \"…\"` — take joint ownership, then retry.")
    lines.append("  3. Work elsewhere, or ask the user to decide which session owns it.")
    _log("block", session=sid, path=path, peers=[p["session_id"] for p, _ in hits])
    return "\n".join(lines)


# ------------------------------------------------------------------------- cli


def _ancestry() -> list[int]:
    pids, p = [], os.getpid()
    for _ in range(8):
        try:
            p = int(Path(f"/proc/{p}/stat").read_text().split()[3])
        except (OSError, ValueError, IndexError):
            break
        if p <= 1:
            break
        pids.append(p)
    return pids


def _resolve_sid(explicit: str | None = None) -> str:
    """Which session is calling this CLI.

    The terminal tool's shell snapshot unsets HERMES_SESSION_*, so the env var is
    usually absent. The hook records the pid of the process that fired it (the
    agent, gateway or cron process), and a command run by that session sits under
    the same pid — so walk our own ancestry and match. Several chats can share one
    gateway pid, in which case the freshest heartbeat among them is the caller:
    its pre_tool_call fired immediately before this command.
    """
    if explicit:
        return explicit
    env = os.environ.get("HERMES_SESSION_ID")
    if env:
        return env
    recs = [r for r in all_records() if is_live(r)]
    if not recs:
        return ""
    live_pids = {int(r.get("pid") or 0) for r in recs}
    for pid in _ancestry():
        if pid in live_pids:
            same = [r for r in recs if int(r.get("pid") or 0) == pid]
            same.sort(key=lambda r: -(r.get("heartbeat") or 0))
            return same[0]["session_id"]
    recs.sort(key=lambda r: -(r.get("heartbeat") or 0))
    return recs[0]["session_id"]


def _ago(ts: float) -> str:
    d = max(0, int(_now() - float(ts or 0)))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    return f"{d // 3600}h"


def cmd_list(as_json: bool = False) -> int:
    me = _resolve_sid()
    recs = live_records()
    if as_json:
        print(json.dumps({"me": me, "sessions": recs}, indent=2, ensure_ascii=False))
        return 0
    if not recs:
        print("no live Hermes sessions registered (this session has not been hooked yet)")
        return 0
    for r in recs:
        mark = "*" if r["session_id"] == me else " "
        print(f"{mark} {r['session_id']}  {_title(r)}")
        bits = [f"    {r.get('surface') or '?'}", f"active {_ago(r.get('heartbeat'))} ago",
                f"turns {r.get('turns', 0)}", f"tools {r.get('tools', 0)}"]
        print("    " + " · ".join(bits))
        claims = _describe_claims(r, limit=8)
        print("    claims: " + (claims or "none yet"))
        un = int(r.get("inbox_read") or 0)
        try:
            pending = max(0, len(_inbox_file(r["session_id"]).read_text(encoding="utf-8").splitlines()) - un)
        except OSError:
            pending = 0
        if pending:
            print(f"    {pending} undelivered message(s)")
    print("\n(* = this session)")
    return 0


def cmd_show(sid: str) -> int:
    rec = load(sid)
    if not rec:
        print(f"no record for {sid}")
        return 1
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    return 0


def cmd_goal(sid: str, text: str) -> int:
    rec = load(sid) or touch(sid)
    rec["goal"] = " ".join(text.split())[:GOAL_MAX]
    rec["goal_source"] = "manual"
    save(rec)
    print(f"goal set for {sid}: {rec['goal']}")
    return 0


def cmd_claim(sid: str, path: str, why: str, shared: bool) -> int:
    rec = load(sid) or touch(sid)
    # A manual claim on a directory (or a repo root) is a whole-repo claim; on a
    # file it is a claim on that file.
    resolved = _resolve(path)
    scope = "repo" if resolved and Path(resolved).is_dir() else "file"
    hits = collisions(sid, path, scope)
    if hits and not shared:
        print(_block_message(sid, resolved or path, hits))
        print("\nUse --shared to take joint ownership anyway.")
        return 1
    key = add_claim(rec, path, scope=scope, kind="manual", shared=shared, why=why)
    save(rec)
    if not key:
        print(f"nothing to claim for {path}")
        return 1
    _, _, cpath = key.partition(":")
    print(f"claimed {_short(cpath)} [{scope}]{' (shared)' if shared else ''} for {sid}")
    return 0


def cmd_release(sid: str, path: str) -> int:
    rec = load(sid)
    if not rec:
        print(f"no record for {sid}")
        return 1
    if release_claim(rec, path):
        save(rec)
        print(f"released {_short(_resolve(path) or path)} for {sid}")
        return 0
    print(f"{sid} holds no claim covering {path}")
    return 1


def cmd_say(sid: str, target: str, text: str) -> int:
    rec = load(sid) or touch(sid)
    if not text.strip():
        print("empty message")
        return 1
    if target not in ("all",) and target not in {r["session_id"] for r in live_records()}:
        print(f"warning: {target} is not a live session (a typo lands in a dead inbox, not an error)")
    n = say(target, text, sender=sid, sender_title=_title(rec))
    print(f"delivered to {n} session(s); it arrives at their next turn")
    return 0


def cmd_inbox(sid: str, peek: bool) -> int:
    rec = load(sid)
    if not rec:
        print(f"no record for {sid} (no inbox)")
        return 0
    msgs = unread(rec)
    if not peek and msgs:
        save(rec)
    if not msgs:
        print("inbox empty")
        return 0
    for m in msgs:
        print(f"from {m.get('from')} ({_ago(m.get('ts'))} ago): {m.get('text')}")
    return 0


def cmd_gc(quiet: bool = False) -> int:
    now = _now()
    removed = 0
    for rec in all_records():
        if is_live(rec, now):
            continue
        try:
            _session_file(rec["session_id"]).unlink()
            removed += 1
        except OSError:
            pass
    live = {_safe(r["session_id"]) for r in all_records() if is_live(r, now)}
    dropped = 0
    for f in INBOX.glob("*.jsonl"):
        if f.stem in live:
            continue
        try:
            if now - f.stat().st_mtime > 24 * 3600:  # a resumed session keeps its messages for a day
                f.unlink()
                dropped += 1
        except OSError:
            pass
    if not quiet:
        print(f"pruned {removed} stale session record(s), {dropped} dead inbox(es)")
    return 0


def cmd_status() -> int:
    me = _resolve_sid()
    recs = live_records(exclude=me)
    print(f"peers={len(recs)}" + (f" ids={','.join(r['session_id'] for r in recs[:4])}" if recs else ""))
    return 0


# -------------------------------------------------------------- work records
#
# A session's goal says what it is doing *now*; a work record says what exists, who owns
# it, and what evidence closes it. One file per (slug, session), so there is still exactly
# one writer per file and no locking. The effective state of a slug is simply the newest
# record among the sessions that have touched it: ownership can move without anyone
# editing a file they do not own, and a session that vanishes leaves its last state
# visible instead of erasing the work.
#
# Two rules from the two designs this came out of, both load-bearing:
#   * ownership does not transfer on the offer. A handoff sets `offered_to`; only the
#     accepting session writing its own newer record moves `accepted_by`, and until then
#     the sender stays accountable.
#   * a finish without evidence is not a finish. `finished` requires --evidence, else the
#     record is stored as `unknown`, because "the process stopped" has never meant "done".

WORK_STATES = ("planned", "doing", "waiting", "offered", "finished", "failed", "abandoned")
FINISHED_VISIBLE = 12 * 3600      # how long a closed record keeps telling peers "don't redo this"


def _work_dir() -> Path:
    d = PEERS / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _work_slug(slug: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (slug or "").strip().lower()).strip("-")
    return s[:64]


def _work_file(slug: str, sid: str) -> Path:
    return _work_dir() / slug / f"{_safe(sid)}.json"


def work_records() -> list[dict]:
    """Every record on disk, newest first. An unreadable file is skipped, never raised."""
    out: list[dict] = []
    try:
        paths = list(_work_dir().glob("*/*.json"))
    except OSError:
        return []
    for path in paths:
        rec = _read_json(path)
        if isinstance(rec, dict) and rec.get("slug"):
            out.append(rec)
    out.sort(key=lambda r: -(float(r.get("updated") or 0)))
    return out


def work_effective() -> list[dict]:
    """One row per slug: the newest record, with ownership resolved by acceptance."""
    rows: dict[str, dict] = {}
    for rec in work_records():
        rows.setdefault(str(rec["slug"]), rec)
    for rec in rows.values():
        rec["owner_effective"] = rec.get("accepted_by") or rec.get("owner")
    return list(rows.values())


def work_write(sid: str, slug: str, **fields) -> dict:
    slug = _work_slug(slug)
    path = _work_file(slug, sid)
    rec = _read_json(path)
    if not isinstance(rec, dict) or not rec:
        rec = {"slug": slug, "owner": sid, "state": "doing", "outcome": "", "scope": [],
               "created": time.time(), "history": []}
    rec.setdefault("history", [])
    rec["history"] = (rec["history"] + [{"at": time.time(), "state": rec.get("state"),
                                         "by": sid}])[-12:]
    rec.update({k: v for k, v in fields.items() if v is not None})
    rec["slug"] = slug
    rec["updated"] = time.time()
    rec["session"] = sid
    _atomic_json(path, rec)
    return rec


def cmd_work(pos: list[str], opts: dict, sid: str) -> int:
    verb = pos[0] if pos else "list"
    slug = _work_slug(pos[1]) if len(pos) > 1 else ""

    if verb == "list" or verb == "sheet":
        rows = work_effective()
        if "--json" in opts:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        if not rows:
            print("no work records (start one with `peerctl work start <slug> --outcome ...`)")
            return 0
        now = _now()
        for r in rows:
            age = _ago(float(r.get("updated") or 0))
            owner = str(r.get("owner_effective") or "?")
            state = r.get("state")
            if state in ("doing", "planned", "waiting", "offered"):
                sess = load(owner)
                if not sess or not is_live(sess, now):
                    state = "owner unconfirmed" if state != "offered" else "offered"
            line = f"{r['slug']:<34} {state:<18} {owner}"
            if r.get("accepted_by") and r.get("accepted_by") != r.get("owner"):
                line += f" (from {r.get('owner')})"
            line += f"  {age} ago"
            print(line)
            if r.get("outcome"):
                print(f"    {str(r['outcome'])[:150]}")
            if r.get("evidence"):
                print(f"    evidence: {str(r['evidence'])[:150]}")
        return 0

    if verb == "show":
        if not slug:
            print("usage: peerctl work show <slug>")
            return 1
        rows = [r for r in work_effective() if r["slug"] == slug]
        print(json.dumps(rows[:1] or [], indent=2, ensure_ascii=False))
        return 0

    if not slug:
        print("usage: peerctl work <start|update|finish|offer|accept|list|show> <slug> ...")
        return 1

    if verb == "start":
        outcome = str(opts.get("--outcome") or "").strip()
        if not outcome:
            print("refusing an anonymous start: pass --outcome \"what will be different\"")
            return 1
        # The rehearsal of 9 Nov 2026: two sessions starting the same slug at once both
        # printed success and the sheet showed one owner, so the duplicate work was real
        # and invisible. A scan of this directory costs nothing and is the duplicate-work
        # signal; refusal unless the caller says it is joining on purpose.
        existing = [r for r in work_effective() if r["slug"] == slug]
        if existing:
            cur = existing[0]
            owner = str(cur.get("owner_effective") or "?")
            if owner != sid and cur.get("state") in ("planned", "doing", "waiting", "offered"):
                if "--join" not in opts:
                    print(f"work {slug} is already {cur.get('state')} under {owner} "
                          f"({_ago(float(cur.get('updated') or 0))} ago)"
                          + (f" — {str(cur.get('outcome'))[:110]}" if cur.get("outcome") else ""))
                    print("Join it with `--join` (a contribution, not a second owner), "
                          "pick another slug, or check its evidence first.")
                    return 1
                rec = work_write(sid, slug, outcome=outcome, state="doing", joined_with=owner)
                print(f"work {slug}: joined {owner}'s record as a contributor")
                return 0
        state = str(opts.get("--state") or "doing")
        rec = work_write(sid, slug, outcome=outcome, state=state,
                         scope=[s for s in [opts.get("--scope")] if s and not isinstance(s, bool)])
        print(f"work {slug}: {rec['state']} — {outcome[:90]}")
        return 0

    if verb == "update":
        state = str(opts.get("--state") or "doing")
        if state not in WORK_STATES:
            print(f"unknown state {state!r}; one of {', '.join(WORK_STATES)}")
            return 1
        rec = work_write(sid, slug, state=state, note=opts.get("--note"))
        print(f"work {slug}: {rec['state']}")
        return 0

    if verb == "finish":
        evidence = str(opts.get("--evidence") or "").strip()
        state = str(opts.get("--state") or ("finished" if evidence else "unknown"))
        if state == "finished" and not evidence:
            print("a finish without --evidence is `unknown`, not `finished`: stored as unknown")
        rec = work_write(sid, slug, state=state, evidence=evidence or None,
                         note=opts.get("--note"))
        print(f"work {slug}: {rec['state']}" + (f" — {evidence[:80]}" if evidence else ""))
        return 0

    if verb == "offer":
        target = str(opts.get("--to") or "").strip()
        if not target:
            print("usage: peerctl work offer <slug> --to <agent|session>")
            return 1
        rec = work_write(sid, slug, state="offered", offered_to=target)
        print(f"work {slug}: offered to {target} — {rec.get('owner')} stays accountable until accepted")
        return 0

    if verb == "accept":
        rec = work_write(sid, slug, state="doing", accepted_by=sid)
        print(f"work {slug}: accepted by {sid}")
        return 0

    print("usage: peerctl work <start|update|finish|offer|accept|list|show> <slug> ...")
    return 1


USAGE = __doc__


def _split_args(rest: list[str], value_opts: tuple[str, ...] = (
        "--session", "--why", "--outcome", "--state", "--note", "--evidence", "--to", "--scope")):
    """Tiny option parser: returns (positional, {opt: value}) and drops flag options.

    Words are never silently re-attached to the message: an option's value is
    consumed, so `say X "text" --session Y` keeps the text intact.
    """
    pos, opts = [], {}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in value_opts:
            if i + 1 < len(rest):
                opts[a] = rest[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--"):
            opts[a] = True
            i += 1
            continue
        pos.append(a)
        i += 1
    return pos, opts


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    pos, opts = _split_args(argv[1:])
    sid = _resolve_sid(str(opts.get("--session") or "") or None)

    if cmd == "hook":
        return hook_main([])
    if cmd == "list":
        return cmd_list("--json" in opts)
    if cmd == "status":
        return cmd_status()
    if cmd == "gc":
        return cmd_gc()
    if cmd == "show":
        if not pos:
            print("usage: peerctl show <session>")
            return 1
        return cmd_show(pos[0])
    if cmd == "work":
        try:
            return cmd_work(pos, opts, sid)
        except Exception as exc:  # noqa: BLE001 - a coordination tool never blocks work
            print(f"peerctl work failed ({type(exc).__name__}: {exc}); nothing was recorded")
            return 0
    if cmd in ("goal", "set-goal"):
        text = " ".join(pos)
        if not sid or not text:
            print("usage: peerctl goal \"what this session is doing\" [--session ID]")
            return 1
        return cmd_goal(sid, text)
    if cmd == "claim":
        if not sid or not pos:
            print("usage: peerctl claim <path> [--why text] [--shared] [--session ID]")
            return 1
        return cmd_claim(sid, pos[0], str(opts.get("--why") or ""), "--shared" in opts)
    if cmd in ("release", "unclaim"):
        if not sid or not pos:
            print("usage: peerctl release <path> [--session ID]")
            return 1
        return cmd_release(sid, pos[0])
    if cmd == "say":
        target = "all" if "--all" in opts else (pos[0] if pos else "")
        text = " ".join(pos[1:]) if "--all" not in opts else " ".join(pos)
        if not target or not text.strip():
            print("usage: peerctl say <session|--all> \"message\" [--session ID]")
            return 1
        return cmd_say(sid, target, text)
    if cmd == "inbox":
        return cmd_inbox(sid, "--peek" in opts)
    print(f"unknown command {cmd!r}\n")
    print(USAGE)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
    except Exception as exc:  # a hook must never take a session down with it
        if sys.argv[1:2] == ["hook"]:
            print(f"peerctl hook failed (ignored): {exc}", file=sys.stderr)
            sys.exit(0)
        raise
