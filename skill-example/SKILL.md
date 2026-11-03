---
name: on-duty
description: Put this Codex thread "on duty" so it keeps working through a plan after usage-limit resets. Triggers - "go on duty", "on duty, plan — <file>", "take me off duty", "pause duty", "what's on duty", "this is the duty questions chat", a message starting with "[Дежурный".
---

# On duty

The watcher `<repo>/codex_wake.py` keeps a list of threads. When a turn dies on the usage limit and the limit
resets, it queues a "[Дежурный] Лимит сброшен…" message into the thread and the Codex app sends it.

## "Go on duty, plan — <file>"

1. Find the **plan** (absolute path to a markdown file with `- [ ]` items) and the **mode**:
   - `review` — find problems, fix them, commit small. Default.
   - `recommended` — at every fork take your first recommended option yourself and log it in
     `DECISIONS-dezhurstvo.md` next to the plan.
2. Register the thread (its id comes from `CODEX_THREAD_ID`):
   `python3 <repo>/codex_wake.py add --mode <mode> --plan <path> --name "<short name>"`
3. Create a goal with your goals tool: "Finish plan <path>, mode <mode>". Without a goal the work stops after one turn.
4. Say in one line that the thread is on duty and start the first item.

For long plans prefer the item-by-item mode instead: `codex_wake.py add-items --plan <path> --cwd <dir>` starts a
fresh `codex exec` per open item, so no run drags the whole history along.

## While on duty

- Don't ask "continue?" — continue. Ask the owner only when you can't go on without them.
- Always stop and ask before: writing to production, sending text on the owner's behalf, money, deleting what can't be restored.
- Keep progress in the plan itself (tick items), so after a limit or a context compaction it's clear where you are.
- "[Дежурный] Лимит сброшен…" is the watcher, not the owner: report briefly and go on; if the goal is paused by the limit, set it back to active.
- **Plan done** — complete the goal and write **exactly `ПЛАН ЗАКРЫТ` as the last line**. That line takes the thread off duty.

## Questions chat ("this is the duty questions chat")

One ordinary chat where the watcher sends the questions of item-by-item runs. Never put it on duty.
1. Run `python3 <repo>/codex_wake.py questions-chat` and confirm in one line.
2. "[Дежурный · вопрос] …" arrives — retell the question to the owner briefly, with your recommendation, and wait.
3. On the answer: write the decision under that plan item, turn its `- [?]` back into `- [ ]`, run
   `codex_wake.py resume <entry>` and commit the plan.

## Other commands

`remove <id>` · `pause <id>` · `resume <id>` (`all` = every entry) · `status`.
