# Contributing

The scheduler uses Python's standard library and Bash. It has no package-install step.

## Local checks

Use Python 3.11 or newer:

```bash
python3 -m unittest discover -s tests -v
```

The suite covers parsing, job persistence and migration, session checks, delivery through a fake Herdr transport,
manager navigation, installer behavior and real curses pseudo-terminal flows for Codex and Claude Code. Tests use
temporary state directories and do not send prompts to live agents.

GitHub Actions runs the same suite on Ubuntu and macOS with Python 3.11 and 3.14 for pushes and pull requests.
It can also be started manually from the Actions tab.

## Live checks

On macOS, run `./install.sh` from your checkout and follow [the smoke checklist](docs/REAL-HERDR-SMOKE.md) with
both agents. Installing links the plugin to that checkout, refreshes official integrations and updates the Herdr
shortcut. Automated tests do not certify Herdr popup rendering or live agent delivery.

## Source layout

| File | Responsibility |
| --- | --- |
| `scripts/scheduler_core.py` | Parsing, persistence, target identity and Herdr helpers |
| `scripts/daemon.py` | Polling, readiness checks and prompt delivery |
| `scripts/manager_ui.py` | Curses manager and forms |
| `scripts/open-manager.sh` | Managed Herdr popup and originating pane context |
| `scripts/install_keybind.py` | Install and remove the plugin-owned shortcut |
| `herdr-plugin.toml` | Plugin metadata, actions and popup entrypoint |

Implementation history is in [the notes](docs/IMPLEMENTATION-NOTES.md). The original Codex UX contract is in
[the RFC](docs/RFC-herdr-codex-scheduler-ux.md).

## Changes and releases

Keep changes focused and add regression coverage for delivery or target-identity changes. Preserve the plugin ID
`codex.scheduler`, existing job state and the original target when editing jobs. Normal prompt delivery must use
Herdr's `agent prompt` surface and wait for `idle` or `done`.

When changing the version, update `herdr-plugin.toml`, `manager_ui.VERSION`, the installer message and the changelog
together. Before a release, check CI and run the live smoke checklist. The repository is installable from a clone
with `./install.sh`.
