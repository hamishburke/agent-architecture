# sync — the alignment profile

One item per decision, each with the check that runs on the machine which has to match, and the
step that fixes it. The point is that "are we aligned?" is a command someone can run at 3 a.m.
without asking either human.

## Running it

```sh
python3 sync/align.py check                 # everything
python3 sync/align.py check --only board    # one layer
python3 sync/align.py check --json          # machine-readable
python3 sync/align.py report                # markdown table, for a board record
python3 sync/align.py explain board.log-verifies
```

Exit codes: `0` all checkable items aligned, `1` at least one drift, `2` nothing drifted but at
least one item could not be checked. A cron gate should treat `2` as "look at this", not as a
pass: an item that cannot be checked is an item nobody knows about.

Two environment defaults, because the checks have to find things on a machine that is not ours:
`HERMES_HOME` (`~/.hermes`) and `BOARD_LOG` (`~/Developer/agent-memory`, the clone of the log
repository). Export either before running if a machine keeps them somewhere else.

## What the statuses mean

| Status | Means | What to do |
|---|---|---|
| `aligned` | The check ran and matched | Nothing |
| `drift` | The check ran and did not match. The line under it shows what the machine actually said | Apply the fix, or reply with the case for why this machine differs |
| `unknown` | The check could not run: no command, a timeout, output that is not a number | Fix the check, or say the number is unknown. Never approximate it into a plausible figure |
| `n/a` | The item does not apply to this platform | Nothing; the `applies_to` field decided |

## Adding an item

Copy the shape of an existing one. Four fields do the work:

- `why` — the failure the item prevents. An item without a measured failure behind it is
  decoration, and decoration is what makes a profile stop being read.
- `check.sh` — one shell command, POSIX, no dependencies the machine may not have. It should
  print the observed value rather than a yes/no, so a drift line tells a reader what is there.
- `compare` — `equals`, `regex`, `gte`, `nonempty` or `exit0`. `gte` parses the last line as an
  integer; anything unparseable is `unknown`, not zero.
- `fix` — the step, in one or two sentences. It is prose on purpose in v1.

Every item must be run on a machine where it *should* fail at least once, or the check proves
nothing. The first run of this profile found one drift that was the check's own bug, which is
the argument for writing checks instead of trusting a document.

## What v1 does not do

**It does not apply anything.** There is no `apply`. The fix text is a step a person or an agent
takes after deciding, and the decision is the part worth keeping human-shaped: two of these
items change how another agent behaves, and a script that silently rewires someone else's
harness is not alignment, it is remote control.

The next increment is not `apply everything`. It is `apply` for the subset whose effect is
purely additive and reversible on the target machine — a file that does not exist yet, a config
key that is absent — with the same evidence rule as everything else: run it, then run `check`,
and show both.
