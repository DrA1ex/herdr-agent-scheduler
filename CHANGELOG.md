# Changelog

## Unreleased

- Read the Codex limit reset from "try again at 6:21 PM" (time only) as well as dated forms, including when the message wraps across lines, and scan the last 1000 unwrapped pane lines.
- Prompt field: ↑/↓ and PgUp/PgDn move through multi-line text (↑/↓ leave the field only from its first or last row), clicks place the cursor, the field grows with the popup and shows a scrollbar and line position.

## 0.4.0

- Schedule prompts for Claude Code through the same Herdr delivery path as Codex.
- Save and verify the target agent type before delivery, including when no native session ID is available.
- Show the correct agent in target details, notifications and waiting notes.
- Preserve Codex jobs, history, actions and the `prefix+s` shortcut when upgrading.
- Refresh both official Herdr integrations during installation.
- Add Claude regression tests and terminal UI coverage.
- Add GitHub Actions tests on Ubuntu and macOS with Python 3.11 and 3.14.

## 0.3.2

- Capture native session identity from both Herdr agent and pane snapshots.
- Use a shared adaptive table for schedules and recent history.
- Group, wrap and scroll job details.

## 0.3.1

- Improve the popup presentation and action shortcuts.
- Show the live thread title and reduce Escape-key delay.

## 0.3.0

- Introduce the selectable terminal manager with editable forms and confirmation dialogs.
- Wait for agent readiness before delivery.
- Add persistent history, notifications and optional sidebar metadata.
