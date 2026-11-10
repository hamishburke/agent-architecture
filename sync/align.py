#!/usr/bin/env python3
"""Check a machine against the alignment profile.

Written for Zeta (Will's agent), which follows this side's architecture rather than
co-designing it: one profile of decisions, one check per decision, run on the machine that
has to match. Stdlib only, no network, no writes.

    python3 sync/align.py check            # aligned / drift / unknown / n/a, one line each
    python3 sync/align.py check --only board --json
    python3 sync/align.py report           # markdown, for a board record or a message
    python3 sync/align.py explain <id>     # why the item exists and what the fix is

Exit codes: 0 everything that could be checked is aligned, 1 at least one drift, 2 nothing
drifted but at least one check could not run. Unknown is never a pass.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PROFILE = HERE / "profile.json"
TIMEOUT = 15

ALIGNED, DRIFT, UNKNOWN, NA = "aligned", "drift", "unknown", "n/a"


def load() -> dict:
    return json.loads(PROFILE.read_text(encoding="utf-8"))


def platform_tag() -> str:
    return "darwin" if sys.platform == "darwin" else "linux"


def env() -> dict:
    e = dict(os.environ)
    e.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
    e.setdefault("BOARD_LOG", str(Path.home() / "Developer" / "agent-memory"))
    e["REPO"] = str(REPO)
    return e


def run(sh: str) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["sh", "-c", sh], capture_output=True, text=True,
                           timeout=TIMEOUT, env=env())
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds" % TIMEOUT
    except OSError as exc:                       # no /bin/sh, or it cannot be executed
        return 126, "", str(exc)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def classify(item: dict) -> tuple[str, str]:
    """Return (status, observed). Observed is what the machine actually said."""
    if platform_tag() not in item.get("applies_to", ["linux", "darwin"]):
        return NA, "applies_to %s" % ",".join(item.get("applies_to", []))

    check = item.get("check")
    if not check:
        return UNKNOWN, "no check yet"

    rc, out, err = run(check["sh"])
    if rc == 124 or rc == 126:
        return UNKNOWN, err or ("exit %d" % rc)

    mode = check.get("compare")
    want = check.get("value")

    if mode == "nonempty":
        return (ALIGNED, out[:120]) if out else (DRIFT, "nothing")
    if mode == "equals":
        return (ALIGNED, out) if out == str(want) else (DRIFT, out or "(no output)")
    if mode == "regex":
        return ((ALIGNED, out) if re.search(str(want), out)
                else (DRIFT, out or "(no output)"))
    if mode == "gte":
        try:
            value = int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return UNKNOWN, ("not a number: %r" % out[:60]) if out else "no output"
        return (ALIGNED, str(value)) if value >= int(want) else (DRIFT, str(value))
    if mode == "exit0":
        return (ALIGNED, "exit 0") if rc == 0 else (DRIFT, "exit %d" % rc)
    return UNKNOWN, "unknown compare mode %r" % mode


def select(profile: dict, only: str | None, skip: str | None) -> list[dict]:
    items = profile["items"]
    if only:
        items = [i for i in items if i.get("layer") == only or i.get("id") == only]
    if skip:
        items = [i for i in items if i.get("layer") != skip and i.get("id") != skip]
    return items


def checked(items: list[dict]) -> list[tuple[dict, str, str]]:
    return [(i, *classify(i)) for i in items]


def cmd_check(a) -> int:
    rows = checked(select(load(), a.only, a.skip))
    if a.json:
        print(json.dumps([{"id": i["id"], "layer": i["layer"], "title": i["title"],
                           "status": s, "observed": o} for i, s, o in rows], indent=1))
    else:
        width = max((len(i["id"]) for i, _, _ in rows), default=10)
        for item, status, observed in rows:
            mark = {ALIGNED: "ok  ", DRIFT: "DRIFT", UNKNOWN: "?????", NA: "n/a "}[status]
            line = "%-5s %-*s  %s" % (mark, width, item["id"], item["title"])
            if status != ALIGNED:
                line += "\n        %s -> %s" % (item["id"], observed[:160])
            print(line)
    counts = {s: sum(1 for _, st, _ in rows if st == s) for s in (ALIGNED, DRIFT, UNKNOWN, NA)}
    print("\n%d aligned, %d drift, %d unknown, %d not applicable"
          % (counts[ALIGNED], counts[DRIFT], counts[UNKNOWN], counts[NA]))
    if counts[DRIFT]:
        return 1
    if counts[UNKNOWN]:
        return 2
    return 0


def cmd_report(a) -> int:
    rows = checked(select(load(), a.only, a.skip))
    print("| Layer | Item | Status | What this machine said |")
    print("|---|---|---|---|")
    for item, status, observed in rows:
        cell = observed.replace("|", "\\|")[:90] or "-"
        print("| %s | %s | %s | %s |" % (item["layer"], item["title"], status, cell))
    drift = [i["id"] for i, s, _ in rows if s == DRIFT]
    unknown = [i["id"] for i, s, _ in rows if s == UNKNOWN]
    print()
    if drift:
        print("Drift: %s" % ", ".join(drift))
    if unknown:
        print("Could not check: %s" % ", ".join(unknown))
    if not drift and not unknown:
        print("No drift.")
    return 0


def cmd_explain(a) -> int:
    profile = load()
    for item in profile["items"]:
        if item["id"] == a.id:
            for key in ("title", "layer", "since", "why", "fix"):
                if item.get(key):
                    print("%-8s %s" % (key + ":", item[key]))
            print("%-8s %s" % ("check:", (item.get("check") or {}).get("sh", "-")))
            print("%-8s %s" % ("status:", classify(item)[0]))
            return 0
    print("no item %r. Items:" % a.id)
    for item in profile["items"]:
        print(" -", item["id"])
    return 1


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Check a machine against the alignment profile.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--only", help="one layer or one item id")
        sp.add_argument("--skip", help="one layer or one item id")

    c = sub.add_parser("check")
    common(c)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    r = sub.add_parser("report")
    common(r)
    r.set_defaults(fn=cmd_report)

    e = sub.add_parser("explain")
    e.add_argument("id")
    e.set_defaults(fn=cmd_explain)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
