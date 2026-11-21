# on-duty

A small macOS menu bar app that shows the usage limits of **Claude Code** and **Codex** in one place, plus a
background watcher that keeps Codex working through a plan after the limit resets ("on duty").

Click the moon in the menu bar, look, click away. No numbers in the menu bar itself.

> The interface and the prompts sent to Codex are in Russian, because that's the author's working language.
> Code, comments and this README are in English.

## What it shows

- **Limits.** 5-hour and weekly windows for Codex and for every Claude Code account on the machine, as bars
  (green under 70 %, yellow under 90 %, red above) with the reset time.
  Faint ticks mark every 16 % for Codex and every 14 % for Claude.
  - Codex: read from `codex app-server` (`account/rateLimits/read`), no tokens spent.
  - Claude: the same endpoint `/usage` uses, called with the account's own login from the macOS keychain.
    No tokens spent, at most once per 5 minutes. An expired login is never refreshed; the app just shows older numbers.
- **Tokens since the last reset.** Summed from the local logs (`~/.claude/projects/**/*.jsonl`,
  `~/.codex/sessions/**/*.jsonl`), incrementally, so a refresh is cheap. Shown as total and "new" — without
  cache reads, which are usually ~98 % of the total (the history a model re-reads on every step).
  Several Claude accounts share one logs folder, so Claude is counted as one sum.
- **Mac mode**, a three-way switch:
  - *Normal* — the app never keeps the Mac awake (default);
  - *While on duty* — no idle sleep while some thread is on duty;
  - *Until 8:00* — no idle sleep until the morning, then back to Normal by itself.

  On battery below 25 % the Mac is let go in any mode. The display still sleeps as usual. Below the switch:
  who keeps the Mac awake right now (from `pmset -g assertions`).
- **Duty threads** with their state, a reason for running or waiting, pause/resume/remove, and questions you can
  answer right in the window. Plans closed for more than 24 hours move into the collapsed **Готово** section.
- A built-in help page (the book icon).

## On duty

Codex stops when a limit runs out and doesn't continue by itself after the reset. The watcher does that:

- **Thread mode.** In a Codex thread say "go on duty, plan — `<file>`" (see `skill-example/SKILL.md`). After every
  reset the watcher queues a "limit reset, continue with the plan" message into that thread through `codex queue`,
  and the app sends it.
- **Item-by-item mode** (`codex_wake.py add-items --plan <file> --cwd <dir>`). One long thread re-sends its whole
  history on every step and can burn a 5-hour window in minutes. Here every open `- [ ]` item of the plan gets a fresh
  `codex exec` without history. The plan file is the only memory between runs. A run ends with one of four marker
  lines: item done, item parked with a question (`- [?]`, the duty goes on), stop and wait for you, plan closed.
  Each question gets a new Codex chat; a configured "questions chat" is used only if creating a new chat fails.
  Each run goes to the first free seat, in this order: Codex, Claude Code account 1, Claude Code account 2
  (`claude -p`, a second account is the one with its own `CLAUDE_CONFIG_DIR`). A seat that hits its limit rests
  until its reset and the same item moves on to the next one. Every seat has its own on/off switch in the window
  (or `codex_wake.py seat 1|2|3 on|off`), shared with the account carousel.

Hard stops are written into every prompt: production writes, text sent on your behalf, money, deleting what can't
be restored.

### Answers, history and scheduling reasons

For an item-by-item duty, the window shows the last unanswered question from the plan's `На решение Дамиру`
section. Click **Ответить**, enter your decision and save it. The command-line equivalent is:

```sh
python3 codex_wake.py answer DUTY_ID "Your exact decision"
```

Use the duty ID from `status`. The question must identify one parked `- [?]` item by number, or that item must be
the only parked one. `answer` adds a dated decision under it, changes it to `- [ ]`, replaces the question with
the answer, commits only the plan if it is under Git, and then resumes the duty. Empty answers, missing questions
and ambiguous item references are rejected. The window also rejects an answer if the displayed question has
changed. A failed commit prevents resuming; the original plan is restored unless it changed during the write.

Completed plans stay in the main list for 24 hours after closure, then appear under **Готово**. They remain in
the watch file; nothing is deleted. Text `status` replaces their individual rows with `готово: N`, while
`status --json` puts them in `completed`. Repeating `add` or `add-items` for the same resolved plan path revives
its existing entry, keeping its execution mode and any live run. Different paths remain separate entries.

The watcher saves a `reason` on every tick. The window and `status` show why a duty is running or waiting:
for a seat to reset, for your answer, because it is paused, or because a lower-priority plan is waiting for room.
These reasons use the scheduling state and cached limits without extra API requests. After a watcher upgrade,
new scheduling details appear after its restart and next tick.

## Install

Requirements: macOS 14+, Python 3, the Xcode Command Line Tools (`swiftc`), the ChatGPT desktop app with Codex,
and Claude Code if you want its limits.

```sh
git clone https://github.com/BreraDMR/on-duty.git
cd on-duty
./install.sh            # builds /Applications/Дежурный.app, installs two launchd agents
./install.sh --uninstall
```

Optional settings — environment variables; the daemon and the app are started by launchd, so set them with
`launchctl setenv NAME value` (or in the agent's plist) and restart both:

| Variable | Default | What |
|---|---|---|
| `DUTY_CLAUDE_DIRS` | `~/.claude-account2` | extra Claude Code config dirs (one per account), `:`-separated |
| `DUTY_HANDOFF_DIR` | `~/.claude/tools/handoff` | an account-carousel tool, if you have one; without it that section is hidden |

The command line does everything the menu does: `python3 codex_wake.py --help`.

## Caveats

- The Claude usage endpoint and the Codex app-server calls are internal APIs of those apps, not public ones.
  They can change with any update.
- Raw token counts are not the limit: limits are weighted by model, so "50 %" isn't half of the weekly tokens.
- Tested on one Mac (Apple Silicon) with two Claude Code accounts and one Codex account.
- Not affiliated with Anthropic or OpenAI.

## License

[PolyForm Noncommercial 1.0.0](LICENSE). Free for personal and other noncommercial use; for commercial use,
contact the author.
