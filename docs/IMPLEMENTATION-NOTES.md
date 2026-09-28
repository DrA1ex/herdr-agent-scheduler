# Scheduler implementation notes

## 0.4.0: Claude Code through Herdr

The base for this update is the locally installed 0.3.2 at `~/herdr-codex-scheduler-0.3.2`, copied into this workspace
before editing. The 0.3.1/0.3.2 popup, session fallback, table and Details improvements are preserved.

Both agents use the same Herdr transport: `agent get` plus `pane get` for discovery, then `agent prompt` for delivery.
Herdr's canonical agent identifiers are `codex` and `claude`. Jobs persist that identifier; the worker checks it before
checking native session identity or project cwd. Live detection takes precedence over a leftover native session
reference when deciding which agent occupies a pane. The UI also separates jobs for different agent types in one pane.

Target labels, Details, waiting notes and notifications identify the saved agent. Editing from another agent's pane
retains the original target. Records lacking any agent identity migrate to Codex, preserving old schedules. The
plugin ID, state directory, actions and keybinding remain `codex.scheduler`. The installer refreshes both official
Herdr integrations.

Validation includes the existing 0.3.2 suite, Claude lifecycle/delivery and agent-replacement regressions, plus a real
curses PTY flow for each agent with a fake Herdr executable. The PTY test now waits for saved state rather than a
Prompt label that curses may not redraw, and drains terminal output while waiting for shutdown on macOS.

The installed Herdr 0.9.0 API schema and official [CLI](https://herdr.dev/docs/cli-reference/) and
[socket API](https://herdr.dev/docs/socket-api/) documentation were checked. The earlier environment limitations
below describe the original 0.3.0 pass. This update runs on macOS with Herdr available; no live Claude pane was present
for a full end-to-end Claude delivery check. Use `REAL-HERDR-SMOKE.md` for that remaining manual verification.

## Original 0.3.0–0.3.2 work

This pass started from the supplied `herdr-codex-scheduler-0.2.4` implementation and the UX RFC dated 2026-09-16.

## Audit findings in 0.2.4

The persistence and target-safety foundations were mostly sound, but the manager did not satisfy the requested UX:

- the main manager was still a command prompt (`n`, `e 1`, `c 1`, ...);
- Edit used an empty `When` field as the hidden "keep old time" convention;
- Cancel/Delete confirmations required typing `y`;
- Esc behavior depended on a 350 ms double-key detector;
- mouse support was absent;
- job creation had no clean ambient visibility outside the manager;
- the daemon sent `agent prompt` regardless of the current agent state;
- an unexpected curses exception could make the popup disappear without leaving a useful on-screen diagnostic;
- `prefix+s` launched `manager_ui.py` as an ad-hoc custom popup, outside the managed plugin runtime environment.

## 0.3.1 UX follow-up

A real Herdr run of 0.3.0 exposed four presentation/input issues that were not visible in the generic PTY test:

- action hotkeys existed but were invisible; buttons now render `[N] New`, `[D] Delete`, `[V] Details`, etc.;
- the root TUI duplicated the Herdr-owned popup title; the internal root title was removed;
- the main view conflated native `agent_session` identity with the human Codex thread name; it now shows Herdr's live
  `terminal_title_stripped` as `Thread`, while Details keeps native `Session ID` separate;
- the apparent Esc pause was ncurses' normal escape-sequence ambiguity delay, not the old scheduler double-Esc logic;
  `ESCDELAY` is now set to 25 ms.

The TUI now uses the terminal's ANSI palette for restrained accent text and keeps the default terminal background.
Herdr's current plugin/socket API does not expose the exact active popup chrome accent color, so the plugin deliberately
does not parse/copy Herdr theme definitions or client config. That would be incorrect for auto-switching and remote
clients.

## 0.3.2 list / identity / Details follow-up

A real 0.3.1 run exposed three more issues:

- new jobs could still show no Session ID because target discovery only consumed `agent get`; 0.3.2 also reads the
  underlying `pane get` snapshot and merges pane-owned `agent_session` / cwd / title fields when the agent view omits
  them. The daemon uses the same merged snapshot before delivery, so display and safety validation use one identity
  source model;
- Scheduled and Recent rows had different implicit field orders and no column header. They now share one adaptive
  table header (`ID`, `When`, `In`, `Status`, optional `Target`, `Prompt`);
- Details was a flat list and truncated long Prompt values. It is now grouped into Schedule, Prompt, Target, Delivery
  and Metadata sections; free-form values wrap, and the view scrolls when content exceeds the popup height.

If Herdr has no native session reference on either `agent get` or `pane get`, the plugin still does not guess one from
Codex files or terminal text. Details explicitly reports that identity was not captured/reported. The installer now
installs/refreshes the official `herdr integration install codex` hook, which is the documented source of native Codex
session identity. Existing Codex processes may need a new session after first installing the hook.

## Implemented

### Popup lifecycle

`prefix+s` now invokes `codex.scheduler.manage` as a `plugin_action`. The action opens the manifest `manager` entrypoint as a managed plugin popup and passes the originating pane through `CODEX_SCHED_TARGET_PANE`. This gives the manager Herdr's normal plugin environment, including `HERDR_PLUGIN_STATE_DIR`, instead of depending on the fallback path used by an ad-hoc custom popup.

### Manager

The manager is now a selectable curses TUI:

- highlighted job selection;
- visible New/Edit/Cancel/Delete/Details/Close actions;
- arrow and Tab navigation;
- New/Edit forms with normal editable controls;
- selectable No/Yes confirmation dialogs;
- Details view;
- immediate post-save selection and confirmation;
- five-row recent history;
- current-target / other-target grouping;
- graceful clipping in narrow terminals;
- optional mouse row/button/field/dialog interaction;
- one-level Esc semantics, with no double-Esc timer.

The legacy letter keys remain only as optional shortcuts.

### Delivery

Delivery now deliberately defers while Herdr reports Codex as `working` or `blocked`. It sends only from `idle` or
`done`. Unknown state is retried rather than assumed safe.

The existing session-id and cwd fallback checks remain. UI target matching was tightened as well: a current-target row
must match the saved pane, and if the job has a native session id the current pane must report that same session.

### Visibility

The implementation uses two supported Herdr surfaces:

1. `herdr notification show` for creation/edit/cancel/delivered/stale/failed events, subject to the user's toast config;
2. `pane report-metadata --token scheduler=...` for a non-invasive `$scheduler` display token.

No user-owned label/title is modified. Because custom metadata tokens do not appear in the default Agent sidebar
layout, persistent ambient visibility requires the user to opt into `$scheduler` in their sidebar rows. The installer
does not alter that layout automatically.

### Reliability

- daemon startup is serialized with a lock to reduce duplicate-worker races;
- manager crashes are appended to `ui.log`;
- after a manager crash, the restored terminal prints the log path and waits for Enter instead of disappearing;
- existing jobs remain lock-protected and atomically rewritten;
- metadata is restored from persisted pending jobs after daemon startup/server recovery when possible.

## Fresh Herdr API audit

Current Herdr documentation was checked during this pass. Relevant current behavior:

- direct `type = "popup"` commands receive `HERDR_ACTIVE_PANE_ID`; the plugin now uses a `plugin_action` that opens a managed plugin popup instead;
- popup terminals are session-modal and do not become normal pane/agent targets themselves;
- `notification show` uses the configured Herdr/terminal/system toast delivery;
- pane metadata tokens are display-only and can be rendered as `$name` in custom Agent sidebar rows;
- `agent prompt` can submit input while an agent is already `working`;
- `idle` and `done` are the ready-for-input states documented by Herdr;
- `blocked` rejects `agent prompt`.

That is why this plugin now waits on `working`/`blocked` instead of relying on a successful `agent prompt` call as proof
that the active turn was not disturbed.

## What cannot be certified in this build environment

The development sandbox for this pass is Linux and does not contain the `herdr` binary, so the RFC's real macOS Herdr
acceptance audit could not be performed here. A real pseudo-terminal curses smoke test passes, but that is explicitly
not equivalent to Herdr popup validation.

There is also a narrow state-change race between the final `agent get` ready-state check and the subsequent
`agent prompt` CLI call. Current Herdr exposes no documented atomic "prompt only if still idle/done" precondition.
The implementation avoids intentional submission while `working`, but final confidence about a transition in that
small window requires the real Codex/Herdr smoke test.

See `REAL-HERDR-SMOKE.md`.
