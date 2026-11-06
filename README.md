# on-duty

A small macOS menu bar app that shows the usage limits of **Claude Code** and **Codex** in one place, plus a
background watcher that keeps Codex working through a plan after the limit resets ("on duty").

Click the moon in the menu bar, look, click away. No numbers in the menu bar itself.

> The interface and the prompts sent to Codex are in Russian, because that's the author's working language.
> Code, comments and this README are in English.

## What it shows

- **Limits.** 5-hour and weekly windows for Codex and for every Claude Code account on the machine, as bars
  (green under 70 %, yellow under 90 %, red above) with the reset time.
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
- **Duty threads** with their state, pause/resume/remove, and a banner when a question is waiting for you.
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
  Questions go to one Codex chat you pick as the "questions chat".
  Each run goes to the first free seat, in this order: Codex, Claude Code account 1, Claude Code account 2
  (`claude -p`, a second account is the one with its own `CLAUDE_CONFIG_DIR`). A seat that hits its limit rests
  until its reset and the same item moves on to the next one. Every seat has its own on/off switch in the window
  (or `codex_wake.py seat 1|2|3 on|off`), shared with the account carousel.

Hard stops are written into every prompt: production writes, text sent on your behalf, money, deleting what can't
be restored.

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
