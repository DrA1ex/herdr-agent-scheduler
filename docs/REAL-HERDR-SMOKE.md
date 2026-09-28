# Real Herdr smoke checklist for 0.4.0

Run this on macOS using the actual terminal and Herdr build. Repeat the agent-specific checks for both Codex CLI and
Claude Code. The Codex wording below applies to either agent.

## Claude Code and upgrade checks

- [ ] Upgrade from 0.3.2; existing Codex jobs, history and `prefix+s` remain available under `codex.scheduler`.
- [ ] Focus Claude Code, open the manager and verify the target and Details show `Claude Code`.
- [ ] Create, edit, cancel and deliver a Claude job; notifications identify Claude Code.
- [ ] Edit a Claude job while focused on Codex; the original Claude pane/session remains the target.
- [ ] A Claude job due during `working` or `blocked` waits, then delivers once from `idle` or `done`.
- [ ] Change Claude's native session ID in the same pane; the job becomes `stale`.
- [ ] Replace Claude with Codex or Codex with Claude in the same pane, including without native session identity;
      the old job becomes `stale` and its prompt is not submitted.

## Opening / popup lifecycle

- [ ] Focus a live Codex pane and press `prefix+s` 10 times across repeated open/close cycles.
- [ ] Confirm the Scheduler never flashes and disappears.
- [ ] Open while Codex is `idle`, `working`, `done` and `blocked`.
- [ ] Resize the outer terminal while the Scheduler is open.
- [ ] Confirm the target at the top is the underlying pane that launched the popup.
- [ ] Confirm there is only one `Codex & Claude Scheduler` title: Herdr's popup header, not a duplicate inside the TUI.
- [ ] Confirm the `Thread` line contains the live Codex thread/title rather than `Session not reported`.
- [ ] Create a fresh job and open Details; confirm `Session ID` contains the native Codex session UUID when `herdr pane get <pane>` reports `agent_session`.
- [ ] Confirm the managed popup receives `HERDR_PLUGIN_STATE_DIR` and the originating pane resolves through `CODEX_SCHED_TARGET_PANE` / plugin context.
- [ ] If any close is unexpected, inspect both `ui.log` and `herdr plugin log list --plugin codex.scheduler --limit 100`.

## Keyboard UX

- [ ] `↑` / `↓` selects job rows and the highlight stays visible.
- [ ] `Tab` moves to actions; `←` / `→` moves among enabled actions.
- [ ] Enter on a pending row opens Edit; Enter on a history row opens Details.
- [ ] New/Edit Backspace, Delete, Left/Right, Home/End, Ctrl+A/E/U/W all behave normally.
- [ ] Esc from New/Edit returns to the main manager without saving.
- [ ] A second ordinary Esc closes the main manager; no timing is required.
- [ ] Cancel/Delete confirmations work with arrows/Tab + Enter and default to No.
- [ ] Confirm visible action labels include `[N]`, `[E]`, `[C]`, `[D]`, `[V]`, and `[Q]` hotkeys.
- [ ] From Details or a form, press Esc once and confirm the parent view appears without a perceptible ~1 s pause.

## CRUD / persistence

- [ ] Create `15h` + default `Continue`.
- [ ] Create an absolute-time job.
- [ ] Confirm the new row is immediately visible and selected after Save.
- [ ] Confirm a single table header labels `ID`, `When`, `In`, `Status`, `Prompt` (and `Target` when wide enough) for both Scheduled and Recent rows.
- [ ] Open a job with a long prompt in Details; confirm the full prompt wraps and the Details view scrolls without truncation.
- [ ] Close and reopen; both jobs remain.
- [ ] Edit one and verify the target session/project does not change.
- [ ] Cancel one; verify it becomes history.
- [ ] Delete one; verify it disappears permanently.
- [ ] Detach/reattach the client and verify pending jobs remain.
- [ ] Restart the Herdr client and verify pending jobs remain.
- [ ] Restart the Herdr server and document whether plugin state/startup restoration behaves as expected.

## Delivery

Use a short safe schedule for these tests.

- [ ] Due while Codex is idle: prompt reaches the exact original live Codex session.
- [ ] Verify no second Codex process is launched.
- [ ] Due while Codex is working: prompt is not submitted during the active turn.
- [ ] After the active turn settles to idle/done, verify the job is delivered once.
- [ ] Due while Codex is blocked: prompt waits and does not answer/escape the dialog.
- [ ] Replace/restart Codex in the same pane with a different native session id: job becomes `stale`.
- [ ] If testing without native session identity, change project cwd: job becomes `stale`.
- [ ] Force a transient Herdr failure and verify retry/error visibility.

Pay special attention to the transition from `working` to `idle/done`. The implementation checks readiness immediately
before `agent prompt`, but Herdr currently exposes no documented atomic state precondition for that prompt call.

## Mouse

Mouse is an enhancement, not a release blocker.

- [ ] Click a row to select it.
- [ ] Click New/Edit/Cancel/Delete/Details/Close.
- [ ] Click When/Prompt fields to focus them.
- [ ] Click Save/Cancel and confirmation choices.
- [ ] Mouse wheel moves through the selectable jobs.
- [ ] If Herdr routes mouse events inconsistently, confirm every workflow still works keyboard-only.

## Visibility

- [ ] With Herdr toasts enabled, create/edit/cancel and delivery events produce appropriate notifications.
- [ ] Add `$scheduler` to a custom Agent sidebar row and verify a pending job shows the timer token.
- [ ] Cancel/delete/fire the last pending job for a pane and verify the token clears.
- [ ] Confirm pane name, Codex terminal title and semantic agent state are never overwritten by the plugin.
