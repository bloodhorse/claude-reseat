---
name: reseat
description: Carry the current Claude Code session into a fresh one that keeps every word of the conversation and drops the dead weight (thinking, tool output, attachments, re-injected context), with every carry verified. Use when the user says the session is heavy, slow, expensive or close to auto-compact, or asks to reseat, slim, lighten or carry over this session without compacting it.
argument-hint: "[talk | work [N]]"
---

# reseat

`reseat` reads this session's transcript (never writing to it), builds a new session under a
fresh id with the conversation and without the weight, checks the copy, and prints the line
that resumes it. The user then leaves this session and opens the copy.

## pick the mode

- **talk**: only the words, the user's and yours. Right for discussion, planning, design,
  long conversations. Lightest.
- **work**: the words plus every tool call, each result cut to its first N characters
  (default 300). Right for coding sessions, where remembering which files were touched and
  what commands ran matters.

If the user gave an argument (`$ARGUMENTS`), obey it. If not, pick by what this session has
mostly been, and say which one you picked in your reply.

## run it

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/reseat" "${CLAUDE_SESSION_ID}" --talk --no-open
```

For work mode replace `--talk` with `--work` (or `--work 600` for longer result snippets).

## read the result

- **exit 0**: the output shows `words … ✓ every word`, `shape ✓`, and
  `carry ~X tokens (was ~Y)`. The last line is the resume command. Tell the user in two or
  three lines: how much lighter the copy is, the mode, and the exact resume line. Tell them
  to `/exit` this session and paste the line in their shell. Don't run `claude --resume`
  yourself.
- **exit 1**: a check failed. The new file was moved to the trash and the original was
  never touched. Show the user the failing line.
- **exit 2**: reseat refused, because it met a transcript line kind it doesn't know. It never
  guesses. Show the user the message. The usual fix is to rerun with `--allow "<kind prefix>"`
  (drop that kind) or `--keep "<kind prefix>"` (carry it as is). Ask which before rerunning.

Never edit, move or delete the original transcript.

## extras

- In kitty with remote control on, `--tab` instead of `--no-open` opens the copy in a new
  tab.
- `--list` numbers the turns. `--at N` starts the copy at turn N, and `--drop-turns 12-15,40`
  leaves turns out.
- `--uncompact` goes back through native compactions: the talk from before them returns
  and their summaries go.
- `--weigh` shows what the session's context is made of, without reseating.
- `--letter FILE` puts a note on top of the copy, framed as a summary.
- `--probe` resumes the copy once in a throwaway fork and asks it for the first and last
  thing said, as proof that it remembers.
