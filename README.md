# reseat

A long Claude Code session gets heavy. Thinking blocks, tool results, re-read files,
reminders and hook output pile up until every turn pays for all of it, and then
auto-compact replaces your conversation with a summary of it.

`reseat` takes the other road. It carries the session into a new one under a fresh id,
keeps **every word** you and Claude said, and leaves the weight behind. The original is
only ever read. The copy resumes with your current CLAUDE.md and remembers the whole
conversation, start to finish.

On real sessions:

| session | before | after |
|---|---|---|
| six-day conversation | 627k | ~30k (the floor) |
| work session, talk mode | 354k | 66k |
| work session, work mode | 354k | 146k |

## two modes

- **talk**: only the words. For discussion, planning, design.
- **work**: the words plus every tool call, each result cut to its first N characters
  (default 300). Claude remembers what it did and the start of what it saw.

## every carry is verified

- sha256 of the original before and after: it's never written to.
- words in = words out, counted per speaker. A single lost word fails the run.
- the new transcript's shape is checked against what the API accepts.
- a transcript line kind it doesn't recognise stops the run by name, with nothing
  written. It never guesses. `--allow` / `--keep` decide those cases explicitly.

A failed check moves the new file to the trash and exits 1.

Tested against ~4,800 real transcripts across Claude Code updates: 0 failed checks,
0 crashes.

## install

As a Claude Code plugin:

```text
/plugin marketplace add bloodhorse/claude-reseat
/plugin install reseat@reseat
```

Or copy `skills/reseat/` into `~/.claude/skills/`.

Needs Python 3.9+. Standard library only, no dependencies.

## use

Inside a heavy session:

```text
/reseat
/reseat work
/reseat work 600
```

Claude runs it on the session it's in and hands you one line. `/exit`, paste it, and
you're in the light copy.

From a shell, the same tool:

```bash
python3 skills/reseat/scripts/reseat              # pick a session of this folder from a list
python3 skills/reseat/scripts/reseat last         # the newest one here
python3 skills/reseat/scripts/reseat <id> --weigh # what its context is made of
python3 skills/reseat/scripts/reseat <id> --list  # numbered turns, for --at / --drop-turns
python3 skills/reseat/scripts/reseat -h           # everything else
```

Other options: `--uncompact` (go back through native compactions and bring back the talk
they summarised), `--at N` / `--drop-turns 12-15,40`, `--letter FILE` (a note on top),
`--probe` (resume the copy once in a throwaway fork and ask it for the first and last
thing said).

## after a Claude Code update

The transcript format moves. `python3 tools/sweep.py` dry-runs reseat over every
transcript on your machine and tallies what it refuses, so a new line kind shows up there
before it bites. `python3 -m unittest discover -s tests` runs the synthetic suite.

## license

MIT
