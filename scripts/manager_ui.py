from __future__ import annotations

import curses
import datetime as dt
import json
import os
import subprocess
import sys
import traceback
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_core import (  # noqa: E402
    agent_kind,
    agent_name,
    agent_session,
    cancel_job,
    create_job,
    delete_job,
    is_supported_agent,
    job_agent_kind,
    job_matches_target,
    live_agent_snapshot,
    local_now,
    parse_when,
    read_jobs,
    run_herdr,
    show_notification,
    sync_schedule_metadata,
    target_snapshot,
    update_job,
)

VERSION = "0.4.0"
HISTORY_LIMIT = 5
ESCDELAY_MS = 25
ACTION_HOTKEYS = {
    "New": "N",
    "Edit": "E",
    "Cancel": "C",
    "Delete": "D",
    "Details": "V",
    "Close": "Q",
}
BUTTONS = tuple(ACTION_HOTKEYS)
ACCENT_PAIR = 1
_accent_attr = 0


class EscapePressed(Exception):
    """Cancel the current child view and return to its parent."""


def _cell_width_char(char: str) -> int:
    if not char or unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _cell_width(text: str) -> int:
    return sum(_cell_width_char(char) for char in text)


def _take_left(text: str, width: int) -> tuple[str, int]:
    out: list[str] = []
    used = 0
    for char in text:
        cells = _cell_width_char(char)
        if used + cells > width:
            break
        out.append(char)
        used += cells
    return "".join(out), used


def _take_right(text: str, width: int) -> tuple[str, int]:
    out: list[str] = []
    used = 0
    for char in reversed(text):
        cells = _cell_width_char(char)
        if used + cells > width:
            break
        out.append(char)
        used += cells
    return "".join(reversed(out)), used


def ellipsis(value: str | None, width: int) -> str:
    text = (value or "-").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    if width <= 0:
        return ""
    if _cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    head, _ = _take_left(text, width - 1)
    return head + "…"


@dataclass
class LineBuffer:
    chars: list[str]
    cursor: int = 0

    @classmethod
    def empty(cls) -> "LineBuffer":
        return cls([], 0)

    @classmethod
    def from_text(cls, text: str) -> "LineBuffer":
        chars = list(text)
        return cls(chars, len(chars))

    @property
    def value(self) -> str:
        return "".join(self.chars)

    def insert(self, text: str) -> None:
        clean = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if not clean:
            return
        chunk = list(clean)
        self.chars[self.cursor:self.cursor] = chunk
        self.cursor += len(chunk)

    def backspace(self) -> None:
        if self.cursor <= 0:
            return
        self.cursor -= 1
        del self.chars[self.cursor]

    def delete(self) -> None:
        if self.cursor < len(self.chars):
            del self.chars[self.cursor]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.chars), self.cursor + 1)

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.chars)

    def clear(self) -> None:
        self.chars.clear()
        self.cursor = 0

    def erase_word(self) -> None:
        start = self.cursor
        while start > 0 and self.chars[start - 1].isspace():
            start -= 1
        while start > 0 and not self.chars[start - 1].isspace():
            start -= 1
        if start != self.cursor:
            del self.chars[start:self.cursor]
            self.cursor = start

    def viewport(self, width: int, placeholder: str = "") -> tuple[str, int]:
        width = max(1, width)
        if not self.chars:
            return ellipsis(placeholder, width), 0

        text = self.value
        left = text[:self.cursor]
        right = text[self.cursor:]
        if _cell_width(text) <= width:
            return text, _cell_width(left)

        left_piece, left_cells = _take_right(left, max(0, width - 1))
        hidden_left = len(left_piece) < len(left)
        prefix = "…" if hidden_left else ""
        cursor_col = _cell_width(prefix) + left_cells

        remaining = max(0, width - cursor_col)
        right_piece, _ = _take_left(right, remaining)
        if len(right_piece) < len(right) and remaining > 0:
            right_piece, _ = _take_left(right, max(0, remaining - 1))
            right_piece += "…"
        return prefix + left_piece + right_piece, cursor_col


@dataclass
class MainState:
    selected_job_id: str | None = None
    focus: str = "list"
    button_index: int = 0
    banner: str | None = None


@dataclass
class DisplayLine:
    text: str
    job_id: str | None = None
    attr: int = 0


def target_pane() -> str | None:
    for name in ("CODEX_SCHED_TARGET_PANE", "HERDR_ACTIVE_PANE_ID", "HERDR_PANE_ID"):
        value = os.environ.get(name)
        if value:
            return value

    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if raw:
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            context = {}
        for key in ("focused_pane_id", "pane_id"):
            value = context.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def parse_timestamp(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or ""))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_now().tzinfo)
    return parsed.astimezone()


def format_due(job: dict[str, Any]) -> str:
    due = parse_timestamp(job.get("due_at"))
    return due.strftime("%Y-%m-%d %H:%M") if due else "?"


def format_edit_when(job: dict[str, Any]) -> str:
    due = parse_timestamp(job.get("due_at"))
    return due.strftime("%Y-%m-%d %H:%M") if due else ""


def format_when_human(value: Any, now: dt.datetime | None = None) -> str:
    stamp = parse_timestamp(value)
    if stamp is None:
        return "?"
    now = (now or local_now()).astimezone()
    day_delta = (stamp.date() - now.date()).days
    clock = stamp.strftime("%H:%M")
    if day_delta == 0:
        return f"Today {clock}"
    if day_delta == 1:
        return f"Tomorrow {clock}"
    if 1 < day_delta < 7:
        return f"{stamp.strftime('%a')} {clock}"
    return stamp.strftime("%b %d %H:%M")


def format_remaining(job: dict[str, Any], now: dt.datetime | None = None) -> str:
    due = parse_timestamp(job.get("due_at"))
    if due is None:
        return "?"
    now = (now or local_now()).astimezone()
    seconds = int((due - now).total_seconds())
    if seconds <= 0:
        return "due"
    minutes = max(1, seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def session_text(session: dict[str, Any] | None) -> str:
    if not session:
        return "—"
    value = session.get("value")
    return str(value) if value is not None else json.dumps(session, ensure_ascii=False)


def thread_text(agent: dict[str, Any] | None) -> str:
    """Return a human-facing agent thread/title without guessing a session id."""
    if not agent:
        return "—"
    for key in ("terminal_title_stripped", "terminal_title", "title", "name", "display_name"):
        value = agent.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "—"


def _init_terminal_style() -> None:
    """Use the terminal palette and keep the Herdr-owned popup background intact."""
    global _accent_attr
    _accent_attr = 0
    try:
        curses.set_escdelay(ESCDELAY_MS)
    except (AttributeError, curses.error):
        pass
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(ACCENT_PAIR, curses.COLOR_BLUE, -1)
        _accent_attr = curses.color_pair(ACCENT_PAIR)
    except (AttributeError, curses.error):
        _accent_attr = 0


def _accent(*, bold: bool = False) -> int:
    return _accent_attr | (curses.A_BOLD if bold else 0)


def project_name(value: str | None) -> str:
    if not value:
        return "-"
    path = Path(value)
    return path.name or str(path)


def job_number(job: dict[str, Any]) -> str:
    value = job.get("number")
    return str(value) if isinstance(value, int) and value > 0 else "?"


def history_time(job: dict[str, Any]) -> Any:
    status = str(job.get("status") or "")
    if status == "sent":
        return job.get("sent_at") or job.get("updated_at")
    if status == "cancelled":
        return job.get("cancelled_at") or job.get("updated_at")
    return job.get("updated_at") or job.get("created_at")


def resolve_target() -> tuple[str | None, dict[str, Any] | None, str | None]:
    pane = target_pane()
    if not pane:
        return None, None, "No active pane was supplied by Herdr."
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    try:
        agent = live_agent_snapshot(herdr, socket_path, pane, runner=run_herdr)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return pane, None, str(exc)
    if not is_supported_agent(agent):
        return pane, agent, "The focused pane is not currently recognized as Codex or Claude Code."
    return pane, agent, None


def compact_target(job: dict[str, Any], width: int = 20) -> str:
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    pane = str(job.get("pane") or target.get("pane") or "?")
    project = project_name(str(target.get("cwd"))) if target.get("cwd") else "-"
    return ellipsis(f"{pane} · {project}", width)


def job_matches_current(job: dict[str, Any], pane: str | None, agent: dict[str, Any] | None) -> bool:
    if not agent or agent_kind(agent) != job_agent_kind(job):
        return False
    session = agent_session(agent) if agent else None
    return job_matches_target(job, pane, session)


def choose_job(jobs: list[dict[str, Any]], token: str | None, pane: str | None,
               agent: dict[str, Any] | None, pending_only: bool = False) -> dict[str, Any] | None:
    candidates = [job for job in jobs if not pending_only or job.get("status") == "pending"]
    if token:
        try:
            number = int(token)
        except ValueError:
            return None
        exact = [job for job in candidates if job.get("number") == number]
        return exact[0] if len(exact) == 1 else None

    current = [job for job in candidates if job_matches_current(job, pane, agent)]
    return current[0] if len(current) == 1 else None


def ensure_daemon() -> None:
    daemon = Path(__file__).resolve().parent / "daemon.py"
    subprocess.run(
        [sys.executable, str(daemon), "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _herdr_context() -> tuple[str, str | None]:
    return os.environ.get("HERDR_BIN_PATH", "herdr"), os.environ.get("HERDR_SOCKET_PATH")


def _refresh_metadata(jobs: list[dict[str, Any]] | None = None) -> None:
    herdr, socket_path = _herdr_context()
    try:
        sync_schedule_metadata(jobs if jobs is not None else read_jobs(), herdr, socket_path)
    except Exception:
        pass


def _notify(title: str, body: str, sound: str = "none") -> None:
    herdr, socket_path = _herdr_context()
    try:
        show_notification(herdr, socket_path, title, body, sound=sound)
    except Exception:
        pass


def _safe_addstr(win: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        height, width = win.getmaxyx()
    except Exception:
        return
    if y < 0 or y >= height or x >= width:
        return
    x = max(0, x)
    room = max(0, width - x - (1 if y == height - 1 else 0))
    if room <= 0:
        return
    clipped, _ = _take_left(text, room)
    try:
        win.addstr(y, x, clipped, attr)
    except curses.error:
        pass


def _draw_rule(win: Any, y: int, char: str = "─") -> None:
    try:
        _, width = win.getmaxyx()
    except Exception:
        return
    _safe_addstr(win, y, 0, char * max(1, width - 1))


def _clear_row(win: Any, y: int) -> None:
    try:
        height, width = win.getmaxyx()
        if 0 <= y < height:
            win.move(y, 0)
            win.addstr(" " * max(0, width - 1))
    except curses.error:
        pass


def _set_cursor(visible: bool) -> None:
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def _field_key(buffer: LineBuffer, key: Any) -> bool:
    if key in (curses.KEY_BACKSPACE, "\x7f", "\b", 127, 8):
        buffer.backspace()
    elif key == curses.KEY_DC:
        buffer.delete()
    elif key == curses.KEY_LEFT:
        buffer.left()
    elif key == curses.KEY_RIGHT:
        buffer.right()
    elif key == curses.KEY_HOME:
        buffer.home()
    elif key == curses.KEY_END:
        buffer.end()
    elif key == "\x01":
        buffer.home()
    elif key == "\x05":
        buffer.end()
    elif key == "\x15":
        buffer.clear()
    elif key == "\x17":
        buffer.erase_word()
    elif isinstance(key, str) and key >= " ":
        buffer.insert(key)
    else:
        return False
    return True


def read_field(win: Any, y: int, label: str, default: str = "", *, prefill: bool = False,
               width: int | None = None) -> str:
    """Compatibility field editor used by tests and small fallback dialogs."""
    buffer = LineBuffer.from_text(default) if prefill and default else LineBuffer.empty()
    try:
        _, screen_width = win.getmaxyx()
    except Exception:
        screen_width = 80
    field_width = width or max(8, screen_width - len(label) - 2)

    while True:
        _clear_row(win, y)
        _safe_addstr(win, y, 0, label, curses.A_BOLD)
        placeholder = "" if prefill else (f"[{default}]" if default else "")
        visible, cursor_col = buffer.viewport(field_width, placeholder)
        _safe_addstr(win, y, len(label), visible, curses.A_DIM if not buffer.value and placeholder else 0)
        try:
            win.move(y, min(screen_width - 1, len(label) + cursor_col))
        except curses.error:
            pass
        win.refresh()
        key = win.get_wch()
        if key in ("\n", "\r", curses.KEY_ENTER):
            return buffer.value if buffer.value else default
        if key in ("\x1b", 27):
            raise EscapePressed
        if key == curses.KEY_RESIZE:
            continue
        _field_key(buffer, key)


def _history_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (job for job in jobs if job.get("status") != "pending"),
        key=lambda job: str(history_time(job) or ""),
        reverse=True,
    )[:HISTORY_LIMIT]


def _job_groups(
    jobs: list[dict[str, Any]], pane: str | None, agent: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    current = sorted(
        (job for job in jobs if job.get("status") == "pending" and job_matches_current(job, pane, agent)),
        key=lambda job: str(job.get("due_at", "")),
    )
    current_ids = {str(job.get("id")) for job in current}
    other = sorted(
        (job for job in jobs if job.get("status") == "pending" and str(job.get("id")) not in current_ids),
        key=lambda job: str(job.get("due_at", "")),
    )
    return current, other, _history_jobs(jobs)


def _pad_cell(value: str, width: int, *, right: bool = False) -> str:
    clipped = ellipsis(value, width)
    padding = max(0, width - _cell_width(clipped))
    return (" " * padding + clipped) if right else (clipped + " " * padding)


def _table_columns(width: int, *, include_target: bool = False) -> list[tuple[str, int]]:
    """Choose one column model shared by Scheduled, Other targets and Recent."""
    if include_target and width >= 82:
        fixed = [("ID", 4), ("When", 15), ("In", 8), ("Status", 9), ("Target", 15)]
    elif width >= 72:
        fixed = [("ID", 4), ("When", 16), ("In", 8), ("Status", 10)]
    elif width >= 54:
        fixed = [("ID", 4), ("When", 14), ("In", 7), ("Status", 9)]
    elif width >= 40:
        fixed = [("ID", 4), ("When", 12), ("Status", 9)]
    else:
        fixed = [("ID", 4), ("Status", 9)]

    gaps = 2 * len(fixed)
    prompt_width = max(6, width - sum(size for _name, size in fixed) - gaps)
    return fixed + [("Prompt", prompt_width)]


def _table_row(
    values: dict[str, str], width: int, *, header: bool = False, include_target: bool = False,
) -> str:
    columns = _table_columns(width, include_target=include_target)
    parts: list[str] = []
    for index, (name, size) in enumerate(columns):
        value = name if header else values.get(name, "")
        is_last = index == len(columns) - 1
        if is_last:
            parts.append(ellipsis(value, size))
        else:
            parts.append(_pad_cell(value, size, right=(name == "ID" and not header)))
    return ellipsis("  ".join(parts), width)


def _job_row(
    job: dict[str, Any], width: int, *, target_value: str = "", history: bool = False,
    include_target: bool = False,
) -> str:
    status = str(job.get("status") or "pending").capitalize()
    when_value = history_time(job) if history else job.get("due_at")
    prompt = str(job.get("prompt") or "")
    values = {
        "ID": job_number(job),
        "When": format_when_human(when_value),
        "In": "—" if history else format_remaining(job),
        "Status": status,
        "Target": target_value,
        "Prompt": prompt,
    }
    return _table_row(values, width, include_target=include_target)


def _build_display_lines(jobs: list[dict[str, Any]], pane: str | None,
                         agent: dict[str, Any] | None, width: int) -> list[DisplayLine]:
    current, other, history = _job_groups(jobs, pane, agent)
    include_target = width >= 82 and bool(other or history)
    lines: list[DisplayLine] = [
        DisplayLine(
            _table_row({}, width, header=True, include_target=include_target),
            attr=curses.A_DIM,
        ),
        DisplayLine("Scheduled", attr=_accent(bold=True)),
    ]
    if current:
        for job in current:
            target_value = "current" if include_target else ""
            lines.append(DisplayLine(
                _job_row(job, width, target_value=target_value, include_target=include_target),
                str(job.get("id")),
            ))
    else:
        lines.append(DisplayLine("  None for this target", attr=curses.A_DIM))

    if other:
        lines.append(DisplayLine(""))
        lines.append(DisplayLine("Other targets", attr=_accent(bold=True)))
        for job in other:
            target_value = compact_target(job, 15) if include_target else ""
            lines.append(DisplayLine(
                _job_row(job, width, target_value=target_value, include_target=include_target),
                str(job.get("id")),
            ))

    if history:
        lines.append(DisplayLine(""))
        lines.append(DisplayLine(f"Recent · last {HISTORY_LIMIT}", attr=_accent(bold=True)))
        for job in history:
            if include_target:
                target_value = "current" if job_matches_current(job, pane, agent) else compact_target(job, 15)
            else:
                target_value = ""
            lines.append(DisplayLine(
                _job_row(
                    job, width, target_value=target_value, history=True, include_target=include_target,
                ),
                str(job.get("id")),
            ))
    return lines


def _manager_lines(jobs: list[dict[str, Any]], pane: str | None, agent: dict[str, Any] | None,
                   target_error: str | None, width: int, max_body_lines: int) -> list[str]:
    del target_error
    lines = _build_display_lines(jobs, pane, agent, width)
    return [line.text for line in lines[:max_body_lines]]


def _target_lines(pane: str | None, agent: dict[str, Any] | None, error: str | None,
                  width: int) -> list[str]:
    if agent and is_supported_agent(agent):
        snap = target_snapshot(pane or "?", agent)
        state = str(snap.get("state") or "unknown")
        project = project_name(snap.get("cwd"))
        target = f"Target   {project} · {pane or '?'} · {agent_name(agent_kind(agent))} {state}"
        thread = f"Thread   {thread_text(agent)}"
        return [ellipsis(target, width), ellipsis(thread, width)]
    detail = error or "Focused pane is not a recognized Codex or Claude Code agent."
    return [ellipsis(f"Target   {pane or 'unavailable'}", width), ellipsis(f"Status   {detail}", width)]


def _selectable_ids(lines: list[DisplayLine]) -> list[str]:
    return [line.job_id for line in lines if line.job_id is not None]


def _selected_job(jobs: list[dict[str, Any]], selected_id: str | None) -> dict[str, Any] | None:
    if selected_id is None:
        return None
    return next((job for job in jobs if str(job.get("id")) == selected_id), None)


def _normalize_selection(state: MainState, lines: list[DisplayLine]) -> None:
    ids = _selectable_ids(lines)
    if state.selected_job_id not in ids:
        state.selected_job_id = ids[0] if ids else None


def _move_selection(state: MainState, lines: list[DisplayLine], delta: int) -> None:
    ids = _selectable_ids(lines)
    if not ids:
        state.selected_job_id = None
        return
    try:
        index = ids.index(state.selected_job_id)
    except ValueError:
        index = 0
    state.selected_job_id = ids[(index + delta) % len(ids)]
    state.focus = "list"


def _button_enabled(name: str, job: dict[str, Any] | None,
                    pane: str | None, agent: dict[str, Any] | None) -> bool:
    if name == "New":
        return bool(pane and agent and is_supported_agent(agent))
    if name == "Close":
        return True
    if name == "Details":
        return job is not None
    if name in ("Edit", "Cancel"):
        return bool(job and job.get("status") == "pending")
    if name == "Delete":
        return job is not None
    return False


def _button_layout(width: int) -> list[list[tuple[int, str, int, int]]]:
    rows: list[list[tuple[int, str, int, int]]] = [[]]
    x = 0
    for index, name in enumerate(BUTTONS):
        label = f"[{ACTION_HOTKEYS[name]}] {name}"
        size = len(label)
        if rows[-1] and x + size > max(12, width - 1):
            rows.append([])
            x = 0
        rows[-1].append((index, label, x, x + size - 1))
        x += size + 2
    return rows


def _render_main(
    win: Any, pane: str | None, agent: dict[str, Any] | None, target_error: str | None,
    jobs: list[dict[str, Any]], state: MainState,
) -> tuple[list[DisplayLine], dict[int, str], list[tuple[int, int, int, int]]]:
    win.erase()
    height, width = win.getmaxyx()
    content_width = max(8, width - 1)
    button_rows = _button_layout(content_width)
    footer_height = len(button_rows) + 3
    list_start = 4
    list_height = max(1, height - list_start - footer_height)

    target_lines = _target_lines(pane, agent, target_error, content_width)
    _safe_addstr(win, 0, 0, target_lines[0], _accent(bold=True))
    _safe_addstr(win, 1, 0, target_lines[1], curses.A_DIM)
    _draw_rule(win, 2)

    lines = _build_display_lines(jobs, pane, agent, content_width)
    _normalize_selection(state, lines)
    selected_line = next((i for i, line in enumerate(lines) if line.job_id == state.selected_job_id), 0)
    max_start = max(0, len(lines) - list_height)
    start = min(max(0, selected_line - list_height // 2), max_start)
    visible = lines[start:start + list_height]
    row_hits: dict[int, str] = {}

    for offset, line in enumerate(visible):
        y = list_start + offset
        attr = line.attr
        if line.job_id is not None:
            row_hits[y] = line.job_id
            if line.job_id == state.selected_job_id:
                attr |= curses.A_REVERSE if state.focus == "list" else curses.A_BOLD
        _safe_addstr(win, y, 0, ellipsis(line.text, content_width), attr)

    footer_y = height - footer_height
    if state.banner:
        _safe_addstr(win, footer_y, 0, ellipsis(state.banner, content_width), curses.A_BOLD)
    _draw_rule(win, footer_y + 1)

    current_job = _selected_job(jobs, state.selected_job_id)
    button_hits: list[tuple[int, int, int, int]] = []
    y = footer_y + 2
    for row in button_rows:
        for index, label, x1, x2 in row:
            name = BUTTONS[index]
            enabled = _button_enabled(name, current_job, pane, agent)
            selected = state.focus == "buttons" and index == state.button_index
            if not enabled:
                _safe_addstr(win, y, x1, label, curses.A_DIM)
            elif selected:
                _safe_addstr(win, y, x1, label, curses.A_REVERSE)
            else:
                marker = label[:3]
                _safe_addstr(win, y, x1, marker, _accent(bold=True))
                _safe_addstr(win, y, x1 + len(marker), label[len(marker):])
            button_hits.append((y, x1, x2, index))
        y += 1

    help_text = "↑↓ select  Tab actions  Enter open  R refresh  Esc close"
    _safe_addstr(win, height - 1, 0, ellipsis(help_text, content_width), curses.A_DIM)
    win.refresh()
    _set_cursor(False)
    return lines, row_hits, button_hits


def _draw_form_field(win: Any, y: int, label: str, buffer: LineBuffer, placeholder: str,
                     focused: bool, width: int) -> None:
    label_width = 12
    field_x = min(label_width, max(0, width - 2))
    field_width = max(8, width - field_x - 4)
    text, cursor_col = buffer.viewport(field_width, placeholder)
    attr = curses.A_REVERSE if focused else 0
    _safe_addstr(win, y, 2, f"{label:<10}", curses.A_BOLD)
    _safe_addstr(win, y, field_x, "[", attr)
    _safe_addstr(win, y, field_x + 1, " " * field_width, attr)
    value_attr = attr | (curses.A_DIM if not buffer.value and placeholder else 0)
    _safe_addstr(win, y, field_x + 1, text, value_attr)
    _safe_addstr(win, y, field_x + 1 + field_width, "]", attr)
    if focused:
        try:
            win.move(y, min(width - 2, field_x + 1 + cursor_col))
        except curses.error:
            pass


def _schedule_form(win: Any, pane: str | None, agent: dict[str, Any] | None,
                   job: dict[str, Any] | None = None) -> tuple[str, str | None]:
    editing = job is not None
    if not editing and (not pane or not agent or not is_supported_agent(agent)):
        return "Cannot create a schedule: focus a live Codex or Claude Code pane first.", None

    original_when = format_edit_when(job) if job else ""
    when_buffer = LineBuffer.from_text(original_when if editing else "15h")
    prompt_text = str(job.get("prompt") or "Continue") if job else ""
    prompt_buffer = LineBuffer.from_text(prompt_text) if editing else LineBuffer.empty()
    focus = 0
    error: str | None = None

    while True:
        win.erase()
        height, width = win.getmaxyx()
        content_width = max(8, width - 1)
        title = f"Edit schedule #{job_number(job)}" if editing else "New schedule"
        _safe_addstr(win, 0, 0, title, _accent(bold=True))
        _draw_rule(win, 1)

        target = job.get("target") if editing and isinstance(job.get("target"), dict) else None
        target_pane_id = str(job.get("pane") or "?") if editing else str(pane or "?")
        target_project = project_name(str(target.get("cwd"))) if target and target.get("cwd") else None
        if not target_project and agent:
            target_project = project_name(target_snapshot(pane or "?", agent).get("cwd"))
        _safe_addstr(win, 3, 2, ellipsis(f"Target      {target_project or '-'} · {target_pane_id}", content_width - 2))

        form_y = 6
        _draw_form_field(win, form_y, "When", when_buffer, "15h", focus == 0, content_width)
        _draw_form_field(win, form_y + 2, "Prompt", prompt_buffer, "Continue", focus == 1, content_width)
        examples = "Examples: 15h · 1h30m · 21:30 · fri 18:00 · tomorrow 09:00"
        _safe_addstr(win, form_y + 4, 2, ellipsis(examples, max(8, content_width - 2)), curses.A_DIM)

        if error:
            _safe_addstr(
                win, height - 5, 2, ellipsis(error, max(8, content_width - 2)),
                curses.A_BOLD | curses.A_REVERSE,
            )

        buttons_y = height - 3
        cancel_attr = curses.A_REVERSE if focus == 2 else 0
        save_attr = curses.A_REVERSE if focus == 3 else 0
        cancel_x = max(2, width - 28)
        save_x = max(cancel_x + 12, width - 13)
        _safe_addstr(win, buttons_y, cancel_x, "[ Cancel ]", cancel_attr)
        _safe_addstr(win, buttons_y, save_x, "[ Save ]", save_attr)
        _safe_addstr(win, height - 1, 0, ellipsis("Tab/↑↓ move  Enter activate  Esc back", content_width), curses.A_DIM)
        win.refresh()
        _set_cursor(focus in (0, 1))

        key = win.get_wch()
        if key == curses.KEY_MOUSE:
            try:
                _id, mouse_x, mouse_y, _z, bstate = curses.getmouse()
            except curses.error:
                continue
            if not _is_left_click(bstate):
                continue
            if mouse_y == form_y:
                focus = 0
                continue
            if mouse_y == form_y + 2:
                focus = 1
                continue
            if mouse_y == buttons_y and cancel_x <= mouse_x < cancel_x + len("[ Cancel ]"):
                focus = 2
                key = "\n"
            elif mouse_y == buttons_y and save_x <= mouse_x < save_x + len("[ Save ]"):
                focus = 3
                key = "\n"
            else:
                continue
        if key in ("\x1b", 27):
            selected_id = str(job.get("id")) if job else None
            return "Edit discarded." if editing else "New schedule cancelled.", selected_id
        if key == curses.KEY_RESIZE:
            continue
        if key in ("\t", curses.KEY_DOWN):
            focus = (focus + 1) % 4
            continue
        if key in (curses.KEY_BTAB, curses.KEY_UP):
            focus = (focus - 1) % 4
            continue
        if focus in (0, 1) and _field_key(when_buffer if focus == 0 else prompt_buffer, key):
            error = None
            continue
        if focus in (2, 3) and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            focus = 2 if focus == 3 else 3
            continue
        if key not in ("\n", "\r", curses.KEY_ENTER):
            continue

        if focus == 0:
            focus = 1
            continue
        if focus == 1:
            focus = 3
            continue
        if focus == 2:
            return "Edit discarded." if editing else "New schedule cancelled.", str(job.get("id")) if job else None

        spec = when_buffer.value.strip()
        prompt = prompt_buffer.value.strip() or "Continue"
        if not spec:
            error = "When cannot be empty."
            focus = 0
            continue

        when: dt.datetime | None
        schedule_spec: str | None
        if editing and spec == original_when:
            when = None
            schedule_spec = job.get("schedule_spec") if isinstance(job.get("schedule_spec"), str) else None
        else:
            try:
                when = parse_when(spec)
            except ValueError as exc:
                error = f"Invalid schedule: {exc}"
                focus = 0
                continue
            schedule_spec = spec

        if editing:
            if not update_job(str(job.get("id")), when=when, prompt=prompt, schedule_spec=schedule_spec):
                return f"Could not update schedule #{job_number(job)}; it is no longer pending.", str(job.get("id"))
            ensure_daemon()
            jobs = read_jobs()
            _refresh_metadata(jobs)
            updated = next((item for item in jobs if item.get("id") == job.get("id")), job)
            body = f"#{job_number(updated)} · {format_when_human(updated.get('due_at'))} · {prompt}"
            _notify(f"{agent_name(job_agent_kind(updated))} schedule updated", body)
            return f"Updated #{job_number(updated)} · {format_when_human(updated.get('due_at'))}", str(job.get("id"))

        herdr, socket_path = _herdr_context()
        created = create_job(when, prompt, str(pane), herdr, socket_path, agent, schedule_spec=spec)
        ensure_daemon()
        jobs = read_jobs()
        _refresh_metadata(jobs)
        body = f"#{job_number(created)} · {format_when_human(created.get('due_at'))} · {prompt}"
        _notify(f"{agent_name(job_agent_kind(created))} prompt scheduled", body)
        message = f"Scheduled for {format_when_human(created.get('due_at'))} · #{job_number(created)}"
        return message, str(created.get("id"))


def _confirm(win: Any, title: str, body: str, yes_label: str) -> bool:
    choice = 0
    while True:
        win.erase()
        height, width = win.getmaxyx()
        content_width = max(8, width - 1)
        _safe_addstr(win, 0, 0, title, _accent(bold=True))
        _draw_rule(win, 1)
        _safe_addstr(win, 4, 2, ellipsis(body, max(8, content_width - 2)))
        no_label = "[ No ]"
        yes_text = f"[ {yes_label} ]"
        x = max(2, (width - len(no_label) - len(yes_text) - 4) // 2)
        _safe_addstr(win, 7, x, no_label, curses.A_REVERSE if choice == 0 else 0)
        _safe_addstr(win, 7, x + len(no_label) + 4, yes_text, curses.A_REVERSE if choice == 1 else 0)
        _safe_addstr(
            win, height - 1, 0, ellipsis("←→/Tab choose  Enter confirm  Esc back", content_width), curses.A_DIM,
        )
        win.refresh()
        _set_cursor(False)

        key = win.get_wch()
        if key == curses.KEY_MOUSE:
            try:
                _id, mouse_x, mouse_y, _z, bstate = curses.getmouse()
            except curses.error:
                continue
            if not _is_left_click(bstate) or mouse_y != 7:
                continue
            if x <= mouse_x < x + len(no_label):
                return False
            yes_x = x + len(no_label) + 4
            if yes_x <= mouse_x < yes_x + len(yes_text):
                return True
            continue
        if key in ("\x1b", 27):
            return False
        if key == curses.KEY_RESIZE:
            continue
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT, "\t", curses.KEY_BTAB):
            choice = 1 - choice
            continue
        if key in ("\n", "\r", curses.KEY_ENTER):
            return choice == 1


def _wrap_cells(value: str, width: int) -> list[str]:
    """Wrap free-form text without truncating it, respecting terminal cell width."""
    width = max(1, width)
    text = str(value or "").replace("\t", "    ").replace("\r", "")
    if not text:
        return ["—"]

    result: list[str] = []
    for source_line in text.split("\n"):
        if not source_line:
            result.append("")
            continue
        remaining = source_line
        while _cell_width(remaining) > width:
            piece, _ = _take_left(remaining, width)
            split = piece.rfind(" ")
            if split > 0:
                piece = piece[:split]
            if not piece:
                piece, _ = _take_left(remaining, width)
            result.append(piece.rstrip())
            remaining = remaining[len(piece):].lstrip()
        result.append(remaining)
    return result or ["—"]


def _detail_field(label: str, value: str, width: int) -> list[DisplayLine]:
    prefix = f"  {label:<12} "
    value_width = max(8, width - _cell_width(prefix))
    wrapped = _wrap_cells(value, value_width)
    lines = [DisplayLine(prefix + wrapped[0])]
    continuation = " " * _cell_width(prefix)
    lines.extend(DisplayLine(continuation + line) for line in wrapped[1:])
    return lines


def _session_id_for_job(job: dict[str, Any]) -> str:
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    value = target.get("session_id")
    if value:
        return str(value)
    session = job.get("agent_session") if isinstance(job.get("agent_session"), dict) else None
    if session and session.get("value") is not None:
        return str(session.get("value"))
    return "Not captured / not reported by Herdr"


def _details_lines(job: dict[str, Any], width: int) -> list[DisplayLine]:
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    status = str(job.get("status") or "-").capitalize()
    lines: list[DisplayLine] = [
        DisplayLine(f"Schedule #{job_number(job)} · {status}", attr=_accent(bold=True)),
        DisplayLine(""),
        DisplayLine("Schedule", attr=_accent(bold=True)),
    ]
    lines += _detail_field("Due", format_due(job), width)
    lines += _detail_field("Remaining", format_remaining(job), width)

    lines += [DisplayLine(""), DisplayLine("Prompt", attr=_accent(bold=True))]
    prompt_width = max(8, width - 4)
    for prompt_line in _wrap_cells(str(job.get("prompt") or "Continue"), prompt_width):
        lines.append(DisplayLine(f"  {prompt_line}"))

    lines += [DisplayLine(""), DisplayLine("Target", attr=_accent(bold=True))]
    lines += _detail_field("Thread", str(target.get("title") or "—"), width)
    lines += _detail_field("Session ID", _session_id_for_job(job), width)
    lines += _detail_field("Pane", str(job.get("pane") or target.get("pane") or "—"), width)
    lines += _detail_field("Project", str(target.get("cwd") or "—"), width)
    lines += _detail_field("Agent", agent_name(job_agent_kind(job)), width)

    lines += [DisplayLine(""), DisplayLine("Delivery", attr=_accent(bold=True))]
    lines += _detail_field("Attempts", str(job.get("attempts", 0)), width)
    lines += _detail_field(
        "State", str(job.get("delivery_note") or job.get("delivery_state") or "—"), width,
    )
    lines += _detail_field("Last error", str(job.get("last_error") or "—"), width)

    lines += [DisplayLine(""), DisplayLine("Metadata", attr=_accent(bold=True))]
    lines += _detail_field("Created", format_when_human(job.get("created_at")), width)
    lines += _detail_field("Updated", format_when_human(job.get("updated_at")), width)
    return lines


def _show_details(win: Any, job: dict[str, Any]) -> None:
    scroll = 0
    while True:
        win.erase()
        height, width = win.getmaxyx()
        content_width = max(8, width - 1)
        body_height = max(1, height - 2)
        lines = _details_lines(job, content_width)
        max_scroll = max(0, len(lines) - body_height)
        scroll = min(max(0, scroll), max_scroll)

        for y, line in enumerate(lines[scroll:scroll + body_height]):
            _safe_addstr(win, y, 0, ellipsis(line.text, content_width), line.attr)

        if max_scroll:
            position = f"{scroll + 1}-{min(len(lines), scroll + body_height)}/{len(lines)}"
            help_text = f"↑↓/PgUp/PgDn scroll  Enter/Esc back  ·  {position}"
        else:
            help_text = "Enter/Esc back"
        _safe_addstr(win, height - 1, 0, ellipsis(help_text, content_width), curses.A_DIM)
        win.refresh()
        _set_cursor(False)

        key = win.get_wch()
        if key in ("\n", "\r", curses.KEY_ENTER, "\x1b", 27, "q", "Q"):
            return
        if key == curses.KEY_RESIZE:
            continue
        if key in (curses.KEY_DOWN, "j", "J"):
            scroll = min(max_scroll, scroll + 1)
        elif key in (curses.KEY_UP, "k", "K"):
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + body_height)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - body_height)
        elif key == curses.KEY_HOME:
            scroll = 0
        elif key == curses.KEY_END:
            scroll = max_scroll


def _activate_action(win: Any, action: str, pane: str | None, agent: dict[str, Any] | None,
                     jobs: list[dict[str, Any]], state: MainState) -> bool:
    job = _selected_job(jobs, state.selected_job_id)
    if not _button_enabled(action, job, pane, agent):
        state.banner = f"{action} is not available for the current selection."
        return False

    if action == "Close":
        return True
    if action == "New":
        state.banner, state.selected_job_id = _schedule_form(win, pane, agent)
        return False
    if action == "Edit" and job:
        state.banner, state.selected_job_id = _schedule_form(win, pane, agent, job)
        return False
    if action == "Details" and job:
        _show_details(win, job)
        return False
    if action == "Cancel" and job:
        body = f"#{job_number(job)} · {format_when_human(job.get('due_at'))} · {job.get('prompt') or 'Continue'}"
        if _confirm(win, f"Cancel schedule #{job_number(job)}?", body, "Yes, cancel"):
            if cancel_job(str(job.get("id"))):
                state.banner = f"Cancelled #{job_number(job)}."
                _refresh_metadata()
                _notify(f"{agent_name(job_agent_kind(job))} schedule cancelled", body)
            else:
                state.banner = "Schedule is no longer pending."
        else:
            state.banner = "Cancel aborted."
        return False
    if action == "Delete" and job:
        body = f"#{job_number(job)} · {job.get('status')} · {job.get('prompt') or 'Continue'}"
        if _confirm(win, f"Delete schedule #{job_number(job)} permanently?", body, "Yes, delete"):
            if delete_job(str(job.get("id"))):
                state.banner = f"Deleted #{job_number(job)}."
                _refresh_metadata()
                state.selected_job_id = None
            else:
                state.banner = "Schedule not found."
        else:
            state.banner = "Delete aborted."
        return False
    return False


def _is_left_click(bstate: int) -> bool:
    click_mask = 0
    for name in ("BUTTON1_CLICKED", "BUTTON1_PRESSED", "BUTTON1_RELEASED"):
        click_mask |= int(getattr(curses, name, 0))
    return bool(bstate & click_mask)


def _mouse_event(state: MainState, row_hits: dict[int, str],
                 button_hits: list[tuple[int, int, int, int]]) -> tuple[bool, int | None, int]:
    try:
        _id, x, y, _z, bstate = curses.getmouse()
    except curses.error:
        return False, None, 0

    if hasattr(curses, "BUTTON4_PRESSED") and bstate & curses.BUTTON4_PRESSED:
        return True, None, -1
    if hasattr(curses, "BUTTON5_PRESSED") and bstate & curses.BUTTON5_PRESSED:
        return True, None, 1

    if not _is_left_click(bstate):
        return False, None, 0

    if y in row_hits:
        state.selected_job_id = row_hits[y]
        state.focus = "list"
        return True, None, 0
    for by, x1, x2, index in button_hits:
        if y == by and x1 <= x <= x2:
            state.focus = "buttons"
            state.button_index = index
            return True, index, 0
    return False, None, 0


def _preferred_button(job: dict[str, Any] | None) -> int:
    if job and job.get("status") == "pending":
        return BUTTONS.index("Edit")
    if job:
        return BUTTONS.index("Details")
    return BUTTONS.index("New")


def _move_button(state: MainState, delta: int, job: dict[str, Any] | None,
                 pane: str | None, agent: dict[str, Any] | None) -> None:
    index = state.button_index
    for _ in range(len(BUTTONS)):
        index = (index + delta) % len(BUTTONS)
        if _button_enabled(BUTTONS[index], job, pane, agent):
            state.button_index = index
            return


def _run_manager(win: Any) -> int:
    win.keypad(True)
    win.timeout(-1)
    _init_terminal_style()
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
    except curses.error:
        pass

    ensure_daemon()
    _refresh_metadata()
    state = MainState()

    while True:
        pane, agent, target_error = resolve_target()
        jobs = read_jobs()
        lines, row_hits, button_hits = _render_main(win, pane, agent, target_error, jobs, state)
        state.banner = None
        key = win.get_wch()

        if key in ("\x1b", 27):
            return 0
        if key == curses.KEY_RESIZE:
            continue
        if key == curses.KEY_MOUSE:
            handled, action_index, scroll_delta = _mouse_event(state, row_hits, button_hits)
            if scroll_delta:
                _move_selection(state, lines, scroll_delta)
            elif action_index is not None:
                action = BUTTONS[action_index]
                if _activate_action(win, action, pane, agent, jobs, state):
                    return 0
            elif handled:
                continue
            continue
        if key in ("\t", curses.KEY_BTAB):
            if state.focus == "list":
                state.focus = "buttons"
                state.button_index = _preferred_button(_selected_job(jobs, state.selected_job_id))
            else:
                state.focus = "list"
            continue
        if key == curses.KEY_UP:
            if state.focus == "buttons":
                state.focus = "list"
            else:
                _move_selection(state, lines, -1)
            continue
        if key == curses.KEY_DOWN:
            _move_selection(state, lines, 1)
            continue
        if state.focus == "buttons" and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            delta = -1 if key == curses.KEY_LEFT else 1
            _move_button(state, delta, _selected_job(jobs, state.selected_job_id), pane, agent)
            continue

        shortcut = key.lower() if isinstance(key, str) else None
        shortcut_map = {hotkey.lower(): action for action, hotkey in ACTION_HOTKEYS.items()}
        if shortcut in shortcut_map:
            if _activate_action(win, shortcut_map[shortcut], pane, agent, jobs, state):
                return 0
            continue
        if shortcut == "r":
            continue

        if key not in ("\n", "\r", curses.KEY_ENTER):
            continue
        if state.focus == "buttons":
            action = BUTTONS[state.button_index]
        else:
            job = _selected_job(jobs, state.selected_job_id)
            action = "Edit" if job and job.get("status") == "pending" else "Details"
        if _activate_action(win, action, pane, agent, jobs, state):
            return 0


def _ui_log_path() -> Path:
    value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if value:
        base = Path(value).expanduser()
    else:
        base = Path.home() / ".local" / "state" / "herdr" / "plugins" / "codex.scheduler"
    base.mkdir(parents=True, exist_ok=True)
    return base / "ui.log"


def _log_crash() -> None:
    try:
        with _ui_log_path().open("a", encoding="utf-8") as stream:
            stream.write(f"\n[{local_now().isoformat()}] UI crash\n")
            traceback.print_exc(file=stream)
    except OSError:
        pass


def main() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Agent Scheduler requires an interactive terminal (open it from the Herdr popup).", file=sys.stderr)
        return 2
    try:
        return curses.wrapper(_run_manager)
    except KeyboardInterrupt:
        return 0
    except Exception:
        _log_crash()
        print("Agent Scheduler crashed. Details were written to:", file=sys.stderr)
        print(str(_ui_log_path()), file=sys.stderr)
        print("Press Enter to close this popup.", file=sys.stderr)
        try:
            sys.stdin.readline()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
