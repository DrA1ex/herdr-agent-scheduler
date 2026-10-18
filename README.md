# Herdr Agent Scheduler

[![Tests](https://github.com/DrA1ex/herdr-agent-scheduler/actions/workflows/tests.yml/badge.svg)](https://github.com/DrA1ex/herdr-agent-scheduler/actions/workflows/tests.yml)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-blue)
![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

**Schedule a prompt for your existing Codex or Claude Code session.**

Tell your agent to continue in 15 hours, run tests tonight, or pick up a task tomorrow morning. The scheduler sends
your prompt through [Herdr](https://herdr.dev) to the same open terminal session when the agent is ready.

```text
When    [ 15h      ]
Prompt  [ Continue ]
```

- Works with **Codex CLI** and **Claude Code** in Herdr panes.
- Create, edit, cancel and inspect schedules in a terminal popup.
- Waits while your agent is working or asking for approval.
- Checks the saved agent and session before delivery.
- Keeps schedules and recent history across popup closures and client reconnects.

## Requirements

- macOS with [Herdr](https://herdr.dev) **0.8.0 or newer**.
- Python **3.11 or newer** available as `python3`.
- Codex CLI or Claude Code already running interactively in a Herdr pane.

The agent session and the Herdr server must remain running until delivery. Schedules run locally on your machine.

## Install

```bash
git clone https://github.com/DrA1ex/herdr-agent-scheduler.git
cd herdr-agent-scheduler
./install.sh
```

The installer links the plugin, starts its worker, adds `prefix+s` and installs or refreshes Herdr's official Codex
and Claude integrations. No Python packages are required.

Reload Herdr's configuration with `prefix+Shift+R`, or detach and reattach the client.

## Schedule a prompt

1. Focus your Codex or Claude Code pane.
2. Press **`prefix+s`**. With Herdr's default prefix: `Ctrl+B`, release, then `s`. The installer turns on
   Herdr's `switch_ascii_input_source_in_prefix`, so `s` works in any keyboard layout (macOS).
3. Check the **Target** line, then choose **New**.
4. Enter a time and a prompt, then choose **Save** (Tab to it, then Enter). An empty prompt uses `Continue`.
   In the prompt, Enter starts a new line, so multi-line text can be pasted as is. The prompt shows three lines and scrolls to keep the cursor visible.
5. Close the popup and leave the agent session open.

When the time arrives, the worker waits for `idle` or `done` and submits your prompt through Herdr.

### Time formats

| Input | Meaning |
| --- | --- |
| `15h` | In 15 hours |
| `1h30m` | In 1 hour and 30 minutes |
| `2d4h` | In 2 days and 4 hours |
| `21:30` | Next occurrence of 21:30 |
| `fri 18:00` | Next Friday at 18:00 |
| `tomorrow 09:00` | Tomorrow at 09:00 |
| `2027-01-15 14:30` | A specific future date and time |

Times use your machine's local timezone. Each schedule sends one prompt.

### Manager controls

| Action | Shortcut |
| --- | --- |
| Select a schedule | `↑` / `↓` |
| Move to actions | `Tab` |
| Select an action | `←` / `→` |
| Edit a pending schedule / inspect history | `Enter` |
| New / Edit / Cancel / Delete / Details | `N` / `E` / `C` / `D` / `V` |
| Close | `Q` or `Esc` |

Forms support ordinary cursor movement, Backspace, Delete, Home and End. `Option` (or `Alt`) with `←` / `→` moves by word, and `Option` with Backspace deletes a word; these do not leave the view. `Esc` returns to the previous view. Shortcut letters also work on Russian and other non-Latin keyboard layouts, by key position.

**Cancel** stops delivery and keeps a history record. **Delete** removes the record permanently. Editing a job
preserves its original target even if you open the manager from a different pane.

## Delivery and history

The scheduler saves the pane, agent type and native session ID reported by Herdr. Replacing Codex with Claude Code,
changing the session ID, or changing the captured project marks the job **stale** instead of sending its prompt.

If Herdr has no native session ID, the scheduler checks the project directory when available. A new session of the
same agent in the same directory cannot be distinguished by that fallback. **Details** shows whether an ID was
captured.

Busy or blocked agents keep their jobs pending. Temporary failures are retried every 30 seconds for up to 24 hours
after the due time. A **sent** status means Herdr accepted the prompt submission; it does not mean the task finished.
The manager shows the five most recent completed records.

## Update

```bash
git pull --ff-only
./install.sh
```

Existing Codex schedules from 0.3.x are preserved. The internal plugin ID remains `codex.scheduler` for compatibility.

## Troubleshooting

**The shortcut is already in use.** Open the manager with:

```bash
herdr plugin action invoke codex.scheduler.manage
```

**The agent is not recognized or its session ID is missing.** Check Herdr's integrations:

```bash
herdr integration status
herdr integration install codex
herdr integration install claude
```

An agent that was running before its integration was installed may need a new session to begin reporting identity.

**A job is waiting or failed.** Open **Details** for the delivery note and last error. Worker and UI logs live in
Herdr's per-plugin state directory, usually `~/.local/state/herdr/plugins/codex.scheduler/`:

```text
daemon.log
ui.log
```

Herdr's plugin logs are also available with:

```bash
herdr plugin log list --plugin codex.scheduler --limit 100
```

## Uninstall

```bash
./uninstall.sh
```

This removes the plugin and its shortcut. Saved schedules and history remain on disk.

## Development

Run the tests with `python3 -m unittest discover -s tests -v`. GitHub Actions runs them on Ubuntu and macOS with
Python 3.11 and 3.14, including terminal UI tests. CI uses a fake Herdr transport; live delivery checks use the
[manual smoke checklist](docs/REAL-HERDR-SMOKE.md).

See [Contributing](CONTRIBUTING.md) for the development workflow and [Changelog](CHANGELOG.md) for release notes.
