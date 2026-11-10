# agent-architecture

The architecture this side runs on, written so another agent on another machine can match it
without a conversation.

Hamish owns the direction. This repository is the artefact that makes "is the other side up to
date?" a command rather than a message: a map of the system, a profile of the decisions that
matter, and a checker that runs on whichever machine has to match.

## Start here

```sh
git clone <this repo> && cd agent-architecture
python3 sync/align.py check      # aligned / drift / unknown / n/a, one line per item
python3 sync/align.py report     # the same, as markdown for a board record
python3 sync/align.py explain <id>
```

No dependencies, no network, no writes. Exit 0 = everything checkable is aligned, 1 = drift,
2 = nothing drifted but something could not be checked. **Unknown is never a pass.**

## What is here

| Path | Holds |
|---|---|
| `MAP.md` | The system as measured: eleven layers, the four joins between them, and what is deliberately absent |
| `sync/profile.json` | One item per decision: what it is, why it exists, the check that runs on the machine, the step that fixes it |
| `sync/align.py` | The checker. stdlib only |
| `sync/versions.json` | Which revision of each sibling repository this side is pinned to |
| `sync/README.md` | The contract: what drift means, what v1 does not do |
| `design/` | Designs written from 11 Nov 2026 onward. Older ones stay in the log repository, which keeps their history and the links to them |

## What is deliberately *not* here

- **The board log.** It is a private repository holding the record history; it is cloned
  separately and pointed at, never vendored here. Visibility differs, and a copy would drift.
- **Facts about a person.** Memory content, plans, household context and anything with an
  address, an account or a name in it stays in the private store. This repo carries the
  mechanism and the measurements.
- **Secrets.** Ever. A token in a public repo is a token in a commit.

## Why not submodules

The obvious shape — this repo as a parent, the pieces as submodules — was considered and
rejected. A submodule pins a commit, so the parent reports "up to date" while the piece inside
it is three commits behind: exactly the silent drift this repository exists to remove. Cloning
also stops being one command on a machine that has to run it unattended. Siblings are pinned by
`sync/versions.json`, which is a file a checker can read and diff, instead of git plumbing a
person has to remember.

## The rule for the other side

Its one job is staying aligned, not being brought up to date. So: pull, run `check`, and act —
apply the fix where the fix is a step, or answer with the case for why this machine should
differ. Drift is a question, not a fault. Silence is the only failure, because silence is
indistinguishable from agreement.
