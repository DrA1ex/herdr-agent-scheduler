from __future__ import annotations

import argparse
from pathlib import Path

BEGIN = "# >>> codex.scheduler keybinding >>>"
END = "# <<< codex.scheduler keybinding <<<"
ASCII_PREFIX_KEY = "switch_ascii_input_source_in_prefix"


def config_path() -> Path:
    return Path.home() / ".config" / "herdr" / "config.toml"


def toml_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def block(root: Path) -> str:
    del root
    return f'''{BEGIN}
[[keys.command]]
key = "prefix+s"
type = "plugin_action"
command = "codex.scheduler.manage"
description = "Codex & Claude Scheduler"
{END}
'''


def ensure_ascii_prefix(text: str) -> str:
    """Enable Herdr's ASCII input switch so prefix+s works on any keyboard layout.

    Without it, the `s` after the prefix is typed in the active (e.g. Russian)
    layout and Herdr does not see the binding.
    """
    flag = f"{ASCII_PREFIX_KEY} = true"
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip().startswith(ASCII_PREFIX_KEY):
            lines[index] = flag
            return "\n".join(lines)
    for index, line in enumerate(lines):
        if line.strip() == "[experimental]":
            lines.insert(index + 1, flag)
            return "\n".join(lines)
    separator = "\n\n" if text.strip() else ""
    return text.rstrip() + separator + "[experimental]\n" + flag + "\n"


def install(root: Path) -> int:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = ensure_ascii_prefix(text)
    new_block = block(root)
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        finish = text.index(END, start) + len(END)
        text = text[:start].rstrip() + "\n\n" + new_block + text[finish:].lstrip("\n")
    else:
        if 'key = "prefix+s"' in text:
            print("warning: prefix+s already exists in Herdr config; keybinding not added")
            print("use: herdr plugin action invoke codex.scheduler.manage")
            return 0
        text = text.rstrip() + ("\n\n" if text.strip() else "") + new_block
    path.write_text(text, encoding="utf-8")
    print(f"installed Codex & Claude Scheduler plugin-action keybinding in {path}")
    return 0


def uninstall() -> int:
    path = config_path()
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        return 0
    start = text.index(BEGIN)
    finish = text.index(END, start) + len(END)
    new_text = (text[:start].rstrip() + "\n" + text[finish:].lstrip("\n")).strip()
    path.write_text((new_text + "\n") if new_text else "", encoding="utf-8")
    print(f"removed keybinding from {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    return install(Path(args.root).resolve()) if args.action == "install" else uninstall()


if __name__ == "__main__":
    raise SystemExit(main())
