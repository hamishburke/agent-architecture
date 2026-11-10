# The map

What is running, where its truth lives, and what breaks when it is wrong. Measured on the host
on **10 November 2026**; every number below came from a command, and the command is named so a
reader can disagree with the number rather than with the prose.

This is the whole system, not the memory store. Memory is one row of eleven.

| # | Layer | What it is | Where the truth lives | Measured |
|---|---|---|---|---|
| 1 | **Host** | One machine, Fedora 44, Intel i7-6700K, 15 GiB RAM, AMD RX 7900 XTX, headless and reached over SSH | `inventory/system.md` (private) | 307 G free of 463; kernel 7.2.15 |
| 2 | **Harness** | Hermes Agent — the thing that runs turns, tools, hooks, cron and the desktop app | `~/.hermes/` | v0.23.0; 17 user services running (`systemctl --user`) |
| 3 | **Models** | One pinned cloud model as the default, a second provider for delegated coding, and a local server for anything that must not leave the host | `~/.hermes/config.yaml` | `deepseek-flash` pinned; Codex second; llama-swap on :8080 serving four local models |
| 4 | **Character** | Conduct and voice, versioned as a file, separate from facts by design — a fact in here is contamination | `agent-system/character/` | versioned; linted; swapped by file |
| 5 | **Facts** | Dated, sourced, expiring records with a five-field contract, plus a lexical+vector index over every document on the host | `agent-system/memory/` → `kb.sqlite3` | 123 records; 127 contracted of 20,217 indexed sections |
| 6 | **Procedure** | Skills — the workflows and pitfalls that go wrong without them | `agent-system/skills/` | 159 `SKILL.md` |
| 7 | **Sessions** | Every chat, cron run and subagent is a separate process sharing one filesystem, coordinated by claims and work slips | `~/.hermes/scripts/peerctl.py` | 4 hooks; 14 sessions live at the busiest moment measured |
| 8 | **Board** | Append-only log, one file per writer, exchanged by git; the channel to the other agent | `willhadfield/agent-memory/board/` | 577 records; verify ok; ~60 s delivery |
| 9 | **Jobs** | Scheduled work, each with a monitor script where a quiet tick should cost nothing | `~/.hermes/cron/jobs.json` | 74 jobs, 61 enabled, 8 gated |
| 10 | **Phone** | Conduit: push, chat, voice, cards, and the typed/mic input path | `~/.hermes/plugins/conduit_push/` | live; push body capped ~500 chars, so a tap must open a chat |
| 11 | **Posture** | What is exposed, who may read it, and what is the recovery path | config + archive | dashboard behind basic auth; secrets only in `~/.hermes/.env`; one offsite archive |

## The four joins that matter

A map of parts is not a system. These four are where the parts have to agree, and each has cost
real hours when they did not:

1. **Harness → sessions.** The harness has no cross-session awareness of its own, and its locks
   protect its database rather than your files. Claims are the join.
2. **Facts → the answer.** A store that ranks perfectly is worthless if nothing queries it: a
   record that was the top hit was once answered "I don't remember", because no search ran.
   The hook is the join, not the ranker.
3. **Board → the human.** A record is not delivered until something opens on the phone. Delivery
   is a cursor plus a receipt, and a receipt is evidence, not a message.
4. **Jobs → attention.** A scheduled job that wakes a model to discover nothing changed spends
   the one resource that does not regenerate. The monitor script is the join.

## What is deliberately not here

- **One host.** No redundancy: if it is off, nothing answers and board delivery waits. That is a
  choice — cheap, private, one thing to understand — not a strength.
- **No clock separate from the jobs table.** The schedule is the harness's; there is no second
  scheduler to disagree with it.
- **No server for the board.** It is a git repo. That is why transport is a minute and why there
  is no port to defend.

## Keeping this true

A map is stale the moment a port, a service or a store moves, and a stale map reads as current —
which is worse than none. Two rules:

- Every number here names its command. Re-run it before quoting it.
- The claims we want to *hold* are not prose: they are the items in `sync/profile.json`, each
  with a check that says whether this machine still matches. `python3 sync/align.py check`.

## Where the detail lives

The full inventory, device register and schedule snapshot live in the private store, not here:
they name addresses, accounts and household context that do not belong in a public repo. This
file carries the shape and the joins; those carry the values.
