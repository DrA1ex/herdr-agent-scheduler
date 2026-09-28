# RFC: Herdr Codex Scheduler — Functional Contract and UX Redesign

**Status:** Draft / handoff for independent re-review

**Date:** 2026-09-16

**Current implementation under review:** `herdr-codex-scheduler-0.2.4`
**Primary environment:** macOS, Herdr, Codex CLI running interactively inside a Herdr pane

## 1. Purpose of this RFC

This document defines what the Codex Scheduler is supposed to do, what the current implementation already attempts to do, which UX problems have been observed, and what a redesigned interface should feel like.

This is intentionally **not** a request to keep patching the current UI incrementally. The next agent should first inspect and reproduce the current implementation from a fresh perspective, verify Herdr behavior in a real session, and then decide whether the existing UI layer should be fixed, simplified, or replaced.

The scheduler's underlying idea is simple:

> While an existing Codex CLI process remains open inside Herdr, schedule a future prompt (usually `Continue`) to be delivered back into that exact live Codex session without restarting Codex, using `codex exec`, or maintaining a Codex fork.

Herdr is used as the transport because it can address a live pane/agent and submit terminal input to it.

---

## 2. Product goal

The intended workflow is primarily for situations where a long-running Codex task is approaching a usage/rate-limit boundary or otherwise needs to be resumed later.

Typical flow:

1. Codex is already running and may currently be working.
2. The user opens the Scheduler from Herdr.
3. The user chooses a delay or future time, for example `15h`.
4. The prompt defaults to `Continue`, but can be changed.
5. The schedule is visibly confirmed and associated with the currently focused Codex session.
6. The user returns to the existing Codex pane. The Codex process remains alive.
7. At or after the scheduled time, the scheduler sends the prompt to that same live Codex process/session through Herdr.
8. The user can inspect, edit, cancel, or delete scheduled jobs before they fire.

The scheduler should feel like a small native Herdr utility, not like a command parser hidden inside a popup.

---

## 3. Non-goals

The scheduler should **not**:

- patch or rebuild Codex CLI;
- maintain a private Codex fork;
- start a second Codex process to continue the conversation;
- use `codex exec resume` for normal delivery;
- close or restart the existing Codex CLI process;
- emulate macOS keyboard input through AppleScript/System Events;
- depend on Rust/Cargo;
- replace Herdr itself with a custom terminal multiplexer;
- require the user to remember opaque UUIDs or internal job IDs.

The scheduler may use a small background worker/daemon because a schedule must continue to exist after the management popup closes.

---

## 4. Current architecture (0.2.4)

The next agent should inspect the implementation rather than assume this summary is perfectly accurate. The important current files are:

```text
herdr-plugin.toml
install.sh
uninstall.sh
scripts/
  daemon.py
  install_keybind.py
  manager_ui.py
  open-manager.sh
  scheduler_core.py
tests/
  test_scheduler.py
```

### 4.1 Herdr integration

The plugin id is:

```text
codex.scheduler
```

The normal shortcut installed in Herdr is:

```text
prefix+s
```

With the default Herdr prefix this means:

```text
Ctrl+B, release, S
```

The current keybinding is a Herdr custom command of `type = "popup"` which directly starts `scripts/manager_ui.py` in a popup sized roughly 86% × 78%.

The manifest also exposes plugin actions (`manage`, `schedule`, `jobs`) as compatibility/fallback entry points.

### 4.2 Target resolution

When the manager opens, it attempts to identify the underlying focused Herdr pane using environment/context values such as `HERDR_ACTIVE_PANE_ID`.

It then asks Herdr for the detected agent in that pane and verifies that the agent is Codex.

When possible, a job records:

- Herdr pane id;
- agent type;
- Codex `agent_session` / session id;
- foreground project cwd;
- terminal title;
- agent state at creation time.

### 4.3 Supported time formats

The current parser supports:

```text
15h
1h30m
2d4h
21:30
fri 18:00
friday 18:00
tomorrow 09:00
2026-09-20 14:30
```

Semantics:

- a relative interval starts from the moment the schedule is saved/edited;
- bare `HH:MM` means the next occurrence of that local time;
- weekday + time means the next matching weekday/time;
- explicit date/time must be in the future.

### 4.4 Prompt

The default prompt is:

```text
Continue
```

It should behave as a default/placeholder, not as awkward pre-entered text that the user must manually erase.

### 4.5 Job state

Jobs have stable human-facing numbers:

```text
1, 2, 3, ...
```

The internal UUID is an implementation detail and should never be required in normal UI.

Current statuses include at least:

```text
pending
sent
cancelled
stale
failed
```

Job numbers are intended to be monotonic and not reused after deletion.

### 4.6 Persistence

State is stored in Herdr's per-plugin state directory, with a fallback under:

```text
~/.local/state/herdr/plugins/codex.scheduler/
```

Pending jobs survive manager close and plugin upgrades.

The current implementation keeps all pending jobs and limits stored finished history to the newest 50 records.

The UI should show at most the 5 most recent completed/cancelled/stale/failed records.

### 4.7 Background delivery

A detached Python worker currently polls pending jobs approximately every 5 seconds.

At due time it verifies the target before delivery:

1. the saved pane must still resolve to a Codex agent;
2. if a native Codex session identity was saved, it must still match;
3. if no native session id was available, the saved project cwd is used as a fallback safety check when possible;
4. only then is the prompt sent with Herdr's agent prompt mechanism.

Temporary delivery failures are retried approximately every 30 seconds, with a maximum delivery window of about 24 hours.

A changed Codex session/project should become `stale`, not receive input accidentally.

### 4.8 Delivery semantics when Codex is busy

The product requirement is:

> A scheduled prompt must never interrupt/cancel the currently running Codex turn.

At the due time, either:

- submit through a Herdr/Codex path that reliably queues the prompt as the next user turn; or
- wait until the existing turn is no longer working and then submit it.

The next agent must verify the actual behavior of the current Herdr + Codex versions instead of assuming `agent prompt` while `working` is safe merely because the command returns success.

---

## 5. Current UI implementation and observed problems

The current `0.2.4` manager was rewritten using Python `curses` after earlier hand-written terminal input/redraw implementations caused repeated line-editing bugs.

The current interface is still primarily command driven. The main screen expects strings such as:

```text
n
e 1
c 1
d 1
v 1
r
q
```

This is technically usable but does not match the desired UX.

### 5.1 Observed problem: popup can appear to refresh and immediately disappear

A real user observation with the current build:

> Pressing `Ctrl+B`, then `S` sometimes produces what looks like a refresh/update and then the Scheduler disappears.

This must be reproduced in a **real Herdr client**, not only through unit tests or a standalone PTY.

Do not assume `curses.wrapper()` is automatically correct merely because it works in a generic pseudo-terminal. Check:

- actual Herdr popup lifecycle;
- alternate-screen handling;
- terminal mode restoration;
- whether the process exits unexpectedly;
- plugin logs/stdout/stderr;
- resize events;
- environment passed by a direct `type = "popup"` keybinding;
- behavior with the user's actual terminal + Herdr version.

### 5.2 Observed problem: scheduled job has poor ambient visibility

After creating a schedule, the user does not feel that the system clearly shows that a timer now exists. Reopening the Scheduler just to verify that it is still active is inconvenient.

A successful creation must produce an obvious result and the active schedule must be easy to notice again.

Ideally, while a pending schedule exists for the currently focused Codex session, Herdr should expose a small non-intrusive indicator/badge/status somewhere in normal use.

The implementation mechanism is deliberately **not prescribed** here. The next agent should research what the current Herdr plugin/API surface can support cleanly.

Do **not** silently hijack the user's pane label, tab title, Codex terminal title, or other user-owned labels merely to display the timer.

If Herdr v1 plugins cannot provide a persistent badge/status without intrusive hacks, document that limitation and propose the cleanest available fallback. At minimum:

- job creation must show an unmistakable confirmation;
- the manager must remain usable and display the newly created job immediately;
- reopening the manager must be reliable and fast;
- optional Herdr/system notification/toast mechanisms may be considered for creation/firing/failure, but should not replace a real manager UI.

### 5.3 Observed problem: command-oriented management is awkward

Typing:

```text
e 2
c 1
d 3
v 4
```

is not the desired primary interface.

The user wants a normal terminal UI where jobs are selectable and actions are discoverable.

### 5.4 Historical input/editing fragility

Previous builds had multiple regressions where:

- every typed character appeared on a new line;
- Backspace caused another line to appear;
- redraw behavior differed between a normal PTY and a Herdr popup.

Therefore the next redesign must treat actual Herdr input/rendering as a first-class acceptance environment.

Do not reintroduce a bespoke terminal line editor unless there is an extremely strong reason.

---

## 6. Desired UX

### 6.1 General interaction model

The Scheduler should behave more like a small settings/task manager than a command prompt.

Primary navigation:

- `↑` / `↓` selects rows or menu items;
- `←` / `→` changes button/choice where appropriate;
- `Tab` / `Shift+Tab` moves between controls/fields where appropriate;
- `Enter` activates the selected item;
- `Esc` moves one level back / cancels the current edit;
- pressing `Esc` again from the main Scheduler view closes the popup.

This naturally satisfies the earlier "two Esc to get out" expectation without a timing-sensitive special double-Escape detector:

```text
Edit form  --Esc-->  Main Scheduler  --Esc-->  Codex/Herdr
```

No 350 ms double-key timing should be required for normal navigation.

Keyboard usage must be complete even if mouse support is added.

### 6.2 Mouse support

Mouse interaction is desirable:

- click to select a job;
- click buttons such as New/Edit/Cancel/Delete/Close;
- click fields to focus them if practical;
- mouse wheel to move through a longer list if needed.

However, mouse support is **SHOULD**, not a blocker for the first correct redesign. Current Herdr versions have had mouse-routing edge cases for terminal applications, so the agent must verify real behavior before depending on mouse as the only way to perform an action.

Arrow keys + Enter must always provide the full workflow.

### 6.3 Proposed main screen

The exact styling can change, but the information hierarchy should be approximately:

```text
┌─ Codex Scheduler ─────────────────────────────────────────────────────┐
│                                                                      │
│  Target                                                              │
│  ff5m · w5:p1 · Codex idle                                           │
│  Session: 019f…                                                      │
│                                                                      │
│  Scheduled                                                           │
│                                                                      │
│   >  1   Today 21:40      in 2h 06m   Continue             Pending   │
│      2   Tomorrow 09:00   in 13h      Run tests            Pending   │
│                                                                      │
│  Recent                                                              │
│      7   Today 12:15                  Continue             Sent      │
│      6   Today 10:42                  Run tests            Cancelled │
│                                                                      │
│  [ New ]   [ Edit ]   [ Cancel ]   [ Delete ]   [ Details ]          │
│                                                        [ Close ]      │
│                                                                      │
│  ↑↓ select   Enter open/action   Esc close                           │
└──────────────────────────────────────────────────────────────────────┘
```

Important properties:

- the currently focused Codex target is obvious at the top;
- jobs for that target are the primary list;
- other-target pending jobs may be shown in a secondary section or via an `All` view/filter;
- selection is visually highlighted;
- no internal UUIDs;
- no need to type a job number for common operations;
- status, due time, remaining time and prompt are visible without entering Details;
- layout must degrade gracefully in narrower popups without wrapping garbage onto unrelated rows.

### 6.4 New schedule flow

`New` should open a form/dialog like:

```text
┌─ New schedule ───────────────────────────────────────────────┐
│                                                             │
│  Target      ff5m · w5:p1                                   │
│                                                             │
│  When        [ 15h                                      ]   │
│  Prompt      [ Continue                                  ]   │
│                                                             │
│  Examples: 15h · 1h30m · 21:30 · fri 18:00 · tomorrow 09:00│
│                                                             │
│                         [ Cancel ]   [ Save ]                │
└─────────────────────────────────────────────────────────────┘
```

Requirements:

- `When` accepts the existing time formats;
- `Prompt` defaults semantically to `Continue`;
- the user can replace the prompt normally;
- Tab/arrow navigation should be predictable;
- Enter on `Save` creates the job;
- Esc cancels the form and returns to the main manager without creating anything;
- no raw command syntax is required.

After Save:

- return to the main manager;
- the new job is visible immediately and selected/highlighted;
- show a short success message such as `Scheduled for Today 21:40`;
- do not immediately close the manager unless the user explicitly chooses that workflow later.

### 6.5 Edit flow

Select a pending job and choose `Edit` or press a discoverable shortcut.

The edit form should be the same form as New, pre-populated with the actual current values.

The user should not have to remember that an empty `When` means "keep old time". The field should simply contain the current schedule/due representation in an editable form, or the UI should make "unchanged" explicit.

Save returns to the list with the updated job selected.

Esc discards unsaved edits and returns to the list.

### 6.6 Cancel vs Delete

The distinction must be visible in the UI:

- **Cancel**: stop a pending job from firing but keep a history record (`cancelled`).
- **Delete**: permanently remove the record from scheduler history/state.

For a pending job, Cancel should be the normal destructive action. Delete is secondary and should require confirmation.

For a completed/history row, Delete is available; Edit/Cancel are disabled.

Confirmation should be an actual Yes/No selection/dialog, not a text field requiring the user to type `y`.

Example:

```text
Cancel schedule #2?
Tomorrow 09:00 · Continue

        [ No ]   [ Yes, cancel ]
```

### 6.7 Details view

Details should remain available for debugging and confidence, but should not be required for ordinary use.

It may show:

```text
Job:       2
Status:    pending
Due:       2026-09-17 09:00 (+19h 14m)
Prompt:    Continue

Target
Pane:      w5:p1
Project:   /Users/.../ff5m
Session:   ...
Agent:     Codex

Delivery
Attempts:  0
Last error: —
Created:   ...
Updated:   ...
```

### 6.8 Recent history

Show only the five most recent non-pending records in the normal manager.

A compact history row is enough:

```text
7  Sent       Today 12:15   Continue
6  Cancelled  Today 10:42   Run tests
```

Do not let history dominate the main screen.

The existing on-disk limit of ~50 finished records is acceptable unless there is a better reason to change it.

---

## 7. Visibility outside the manager

This needs explicit investigation.

Desired outcome:

> When there is at least one pending job for the current Codex target, the user should have some non-intrusive way to notice that fact without opening the Scheduler solely to check whether a timer exists.

Preferred information, if the platform supports it cleanly:

```text
⏱ 1 scheduled · next in 2h 06m
```

Potential clean mechanisms to investigate:

- a supported Herdr plugin status/badge surface;
- a non-intrusive Herdr in-app notification/toast after create/edit/cancel/fire/fail;
- a plugin-supported border/status decoration if one exists.

Do not use the following without explicit approval:

- overwriting the user's pane label;
- rewriting the Codex terminal title;
- keeping a permanent extra split/pane open solely as a timer indicator;
- macOS GUI automation.

If no persistent indicator is possible with Herdr's current plugin API, record that as a platform limitation in the implementation notes and provide the least intrusive fallback.

---

## 8. Reliability and safety requirements

### 8.1 Same live Codex

A job must target the live Codex process/session it was created for.

If the pane is reused for another process/session, the scheduler must not blindly type the prompt there.

### 8.2 Never interrupt a turn

Delivery must not cancel, submit Escape to, or otherwise interrupt an active Codex turn.

If prompt submission while Codex is working is verified to queue safely as the next turn, that behavior is acceptable. Otherwise wait for an appropriate non-working state.

### 8.3 Persistence

Pending jobs must survive:

- closing the Scheduler popup;
- detaching and reattaching the Herdr client;
- plugin manager reopening;
- normal plugin upgrade;
- a Herdr client restart, assuming the Herdr server/plugin state persists.

The next agent should separately verify behavior across a full Herdr server restart and document the result.

### 8.4 Concurrency

The manager and daemon may read/write jobs concurrently. Job storage must remain atomic/locked.

### 8.5 Failure visibility

A job that becomes `stale` or `failed` must be visible in recent history, with an understandable reason in Details.

Silent loss is not acceptable.

### 8.6 No accidental target changes during edit

Editing time/prompt should not silently rebind an existing job to whichever pane happens to be focused at edit time. Target rebinding, if ever supported, should be an explicit action.

---

## 9. Required fresh audit before implementation

The next agent should not begin by changing code. First perform this audit on the current `0.2.4` build.

### 9.1 Real Herdr reproduction

Use a real Herdr session on macOS with Codex running in a pane.

Verify:

1. `prefix+s` opens the Scheduler reliably.
2. The popup remains open rather than flashing/disappearing.
3. Reopening it repeatedly works.
4. Opening while Codex is `working` works.
5. Opening while Codex is `idle/done/blocked` works.
6. Resize the outer terminal while the Scheduler is open.
7. Test actual Backspace/Delete/arrows/Enter/Escape in Herdr.
8. Check Herdr plugin logs and scheduler daemon logs after any unexpected close.
9. Verify the popup receives the correct `HERDR_ACTIVE_PANE_ID` and plugin state directory.
10. Verify the target shown by the UI is actually the pane from which the Scheduler was opened.

### 9.2 Current CRUD audit

Using only the existing build:

1. create a job with `15h` + default `Continue`;
2. create another with an absolute time;
3. reopen the manager and verify both persisted;
4. edit one;
5. cancel one;
6. delete one;
7. inspect details;
8. verify human job numbers remain stable;
9. verify history is limited in the UI;
10. verify jobs remain valid after detach/reattach.

### 9.3 Real delivery audit

Use a short schedule such as `1m` or the shortest safe test interval supported after adding a test-only mechanism if needed.

Verify:

- prompt reaches the exact original Codex CLI;
- it appears as normal interactive input in that existing session;
- no second Codex process is launched;
- a changed Codex session is rejected as stale;
- behavior while Codex is working is known and documented;
- failures/retries are observable.

### 9.4 Mouse feasibility audit

Determine whether a terminal UI in the Herdr popup can reliably receive mouse reporting on the actual Herdr/terminal combination.

If reliable, implement click selection as an enhancement.

If not reliable, keep keyboard navigation first-class and do not delay the UX redesign waiting for mouse support.

---

## 10. Acceptance criteria for the redesigned UX

The redesign is acceptable only when all of the following are true.

### Opening and closing

- `prefix+s` opens the manager every time in a real Herdr session.
- It does not flash and disappear unexpectedly.
- `Esc` from a child form/dialog returns to the previous screen.
- `Esc` from the main manager closes it.
- Therefore two ordinary Esc presses from an edit form return all the way to Codex.

### Navigation

- The main list is navigable with arrow keys.
- A selected row is visibly highlighted.
- Enter performs the obvious/default action or opens details/edit through a clear menu.
- Common workflows do not require typing job numbers.
- Buttons/actions are visible and discoverable.

### New job

- New schedule can be created entirely through fields/buttons.
- Existing time formats still work.
- Prompt defaults to `Continue`.
- After Save, the new job is immediately visible and selected.
- User receives clear confirmation.

### Edit

- Select a job and edit it without typing `e N`.
- Fields are normal editable controls.
- Backspace/Delete/arrows work correctly in an actual Herdr popup.
- Esc discards the edit.

### Cancel/Delete

- Cancel and Delete are separate, understandable actions.
- Confirmation uses selectable buttons/options, not a text `y/N` prompt.

### Target confidence

- Main screen clearly states which Codex/project/session is the current target.
- Each "other target" job is identifiable without overflowing/wrapping the layout.
- Details show full technical target information.

### History

- Main screen shows at most five recent finished records.
- History rows are compact.
- Finished history cannot grow unbounded on disk.

### Delivery

- Same live Codex session receives the prompt.
- No Codex fork/patch/rebuild.
- No second Codex process.
- No accidental delivery to a changed session.
- No interruption of an active turn.

### Regression testing

- Unit tests remain useful, but are not sufficient by themselves.
- A real-Herdr smoke checklist must be performed before calling a release fixed.
- Add automated tests for pure state/scheduling logic.
- Keep terminal-specific automated tests where valuable, but do not treat generic PTY/curses success as proof of Herdr compatibility.

---

## 11. Suggested implementation direction (non-binding)

The next agent is free to replace the current `curses` UI if another approach is materially more robust inside a Herdr popup.

A good design should separate:

```text
scheduler_core.py     pure schedule/job/state semantics
worker/daemon         delivery and retry logic
UI adapter            interactive manager only
Herdr integration     target discovery, prompt delivery, optional notifications
```

The UI should consume an explicit state model rather than directly mixing storage, Herdr process calls, rendering, and command parsing.

For example:

```text
SchedulerState
  current_target
  selected_job
  pending_current[]
  pending_other[]
  recent_history[]
  screen = LIST | NEW | EDIT | DETAILS | CONFIRM
```

This makes arrow/menu navigation straightforward and makes the visual layer replaceable without risking the delivery logic.

Do not optimize for preserving the existing `manager_ui.py` structure if replacing it produces a simpler and more reliable result.

---

## 12. Herdr platform notes to verify

As of the current documentation:

- custom command `type = "popup"` is a session-modal terminal surface and receives terminal input until the command exits;
- popup commands use `HERDR_ACTIVE_PANE_ID` for the underlying tiled pane rather than `HERDR_PANE_ID`;
- Herdr is mouse-oriented, and terminal applications can request mouse reporting, but current-version mouse-routing behavior must be tested on the actual environment;
- Herdr supports in-app/terminal/system notification mechanisms for its own notification system; whether plugins can invoke a clean toast/status API directly should be verified instead of assumed;
- `agent prompt` is the intended high-level mechanism for submitting a prompt to a detected live agent.

Useful references for the implementing agent:

- https://herdr.dev/docs/configuration/
- https://herdr.dev/docs/keyboard/
- https://herdr.dev/docs/cli-reference/
- https://herdr.dev/docs/socket-api/

---

## 13. Final product principle

The Scheduler should be boring to use.

The user should be able to think:

```text
Open scheduler → choose New → enter 15h → Continue → Save → done.
```

And later:

```text
Open scheduler → arrow to job → Edit / Cancel / Delete.
```

The implementation details — daemon, session identity, locks, Herdr socket, retries — should be visible only in Details/debugging, not in the everyday interaction model.
