from __future__ import annotations

import curses
import datetime as dt
import json
import os
import pty
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daemon  # noqa: E402
import install_keybind  # noqa: E402
import manager_ui  # noqa: E402
from scheduler_core import (  # noqa: E402
    agent_kind,
    cancel_job,
    create_job,
    delete_job,
    is_codex,
    is_supported_agent,
    job_matches_target,
    live_agent_snapshot,
    locked_jobs,
    parse_codex_reset,
    parse_when,
    read_jobs,
    same_session,
    show_notification,
    sync_schedule_metadata,
    target_snapshot,
    update_job,
)

TZ = dt.timezone(dt.timedelta(hours=3))
NOW = dt.datetime(2026, 9, 9, 22, 0, tzinfo=TZ)


class ParseWhenTests(unittest.TestCase):
    def test_relative_hours(self):
        self.assertEqual(parse_when("15h", NOW), NOW + dt.timedelta(hours=15))

    def test_relative_compound(self):
        self.assertEqual(parse_when("1h30m", NOW), NOW + dt.timedelta(hours=1, minutes=30))

    def test_clock_rolls_to_next_day(self):
        self.assertEqual(parse_when("21:30", NOW), dt.datetime(2026, 9, 10, 21, 30, tzinfo=TZ))

    def test_clock_same_day(self):
        self.assertEqual(parse_when("23:30", NOW), dt.datetime(2026, 9, 9, 23, 30, tzinfo=TZ))

    def test_weekday(self):
        self.assertEqual(parse_when("fri 18:00", NOW), dt.datetime(2026, 9, 11, 18, 0, tzinfo=TZ))

    def test_weekday_rolls_one_week(self):
        friday = dt.datetime(2026, 9, 11, 19, 0, tzinfo=TZ)
        self.assertEqual(parse_when("fri 18:00", friday), dt.datetime(2026, 9, 18, 18, 0, tzinfo=TZ))

    def test_tomorrow(self):
        self.assertEqual(parse_when("tomorrow 09:15", NOW), dt.datetime(2026, 9, 10, 9, 15, tzinfo=TZ))

    def test_absolute(self):
        self.assertEqual(parse_when("2026-09-12 14:20", NOW), dt.datetime(2026, 9, 12, 14, 20, tzinfo=TZ))

    def test_reject_past_absolute(self):
        with self.assertRaises(ValueError):
            parse_when("2026-09-08 14:20", NOW)


CODEX_STATUS = """\
  5h limit:            [███████████████████░] 97% left (resets 05:05)
  Weekly limit:        [███████████░░░░░░░░░] 57% left (resets 01:57 on 25 Oct)
"""
CODEX_WEEKLY_EXHAUSTED = """\
  5h limit:            [░░░░░░░░░░░░░░░░░░░░] 0% left (resets 05:05)
  Weekly limit:        [░░░░░░░░░░░░░░░░░░░░] 0% left (resets 01:57 on 25 Oct)
"""


class CodexResetParseTests(unittest.TestCase):
    def test_undated_5h_reset_is_the_next_clock_time_today(self):
        now = dt.datetime(2026, 10, 19, 0, 10, tzinfo=TZ)
        self.assertEqual(parse_codex_reset(CODEX_STATUS, now), dt.datetime(2026, 10, 19, 5, 5, tzinfo=TZ))

    def test_undated_clock_time_already_passed_means_tomorrow(self):
        now = dt.datetime(2026, 10, 19, 6, 0, tzinfo=TZ)
        self.assertEqual(parse_codex_reset(CODEX_STATUS, now), dt.datetime(2026, 10, 20, 5, 5, tzinfo=TZ))

    def test_dated_5h_reset_uses_the_printed_date(self):
        now = dt.datetime(2026, 10, 18, 21, 40, tzinfo=TZ)
        text = "  5h limit: [..] 4% left (resets 00:01 on 19 Oct)"
        self.assertEqual(parse_codex_reset(text, now), dt.datetime(2026, 10, 19, 0, 1, tzinfo=TZ))

    def test_exhausted_weekly_limit_wins_over_5h(self):
        now = dt.datetime(2026, 10, 19, 0, 10, tzinfo=TZ)
        self.assertEqual(
            parse_codex_reset(CODEX_WEEKLY_EXHAUSTED, now),
            dt.datetime(2026, 10, 25, 1, 57, tzinfo=TZ),
        )

    def test_try_again_message_gives_the_retry_time(self):
        now = dt.datetime(2026, 10, 18, 22, 0, tzinfo=TZ)
        text = "You've hit your usage limit. try again at Oct 19th, 2026 12:01 AM."
        self.assertEqual(parse_codex_reset(text, now), dt.datetime(2026, 10, 19, 0, 1, tzinfo=TZ))

    def test_returns_none_when_dated_reset_already_passed(self):
        now = dt.datetime(2026, 10, 19, 6, 0, tzinfo=TZ)
        self.assertIsNone(parse_codex_reset("  5h limit: [..] 4% left (resets 00:01 on 19 Oct)", now))

    def test_returns_none_without_limit_lines(self):
        now = dt.datetime(2026, 10, 18, 21, 40, tzinfo=TZ)
        self.assertIsNone(parse_codex_reset("just some pane output", now))

    def test_date_in_january_read_from_late_december_moves_to_next_year(self):
        now = dt.datetime(2026, 12, 31, 23, 0, tzinfo=TZ)
        text = "  5h limit: [..] 4% left (resets 00:30 on 1 Jan)"
        self.assertEqual(parse_codex_reset(text, now), dt.datetime(2027, 1, 1, 0, 30, tzinfo=TZ))


class SessionAndTargetTests(unittest.TestCase):
    def test_supported_agents_use_herdr_identity_fields(self):
        for kind in ("codex", "claude"):
            for field in ("agent", "kind", "agent_kind", "label"):
                with self.subTest(kind=kind, field=field):
                    self.assertTrue(is_supported_agent({field: kind}))
            self.assertTrue(is_supported_agent({"agent_session": {"agent": kind, "value": "session"}}))
        self.assertFalse(is_supported_agent({"agent": "gemini"}))
        self.assertFalse(is_supported_agent({"display_agent": "Claude Code", "title": "claude"}))

    def test_live_agent_kind_overrides_old_session_and_display_label(self):
        agent = {"agent": "claude", "label": "codex", "agent_session": {"agent": "codex", "value": "old"}}
        self.assertEqual(agent_kind(agent), "claude")
        self.assertFalse(is_codex(agent))
        self.assertFalse(is_supported_agent({"agent": "gemini", "agent_session": {"agent": "claude"}}))

    def test_claude_session_only_snapshot_records_claude(self):
        session = {"agent": "claude", "kind": "id", "value": "claude-session"}
        target = target_snapshot("w1:p2", {"agent_session": session})
        self.assertEqual(target["agent"], "claude")
        self.assertEqual(target["session_id"], "claude-session")

    def test_same_session(self):
        a = {"agent": "codex", "kind": "id", "value": "abc"}
        b = {"agent": "codex", "kind": "id", "value": "abc"}
        self.assertTrue(same_session(a, b))

    def test_changed_session(self):
        a = {"agent": "codex", "kind": "id", "value": "abc"}
        b = {"agent": "codex", "kind": "id", "value": "def"}
        self.assertFalse(same_session(a, b))

    def test_target_snapshot_shows_session_cwd_title(self):
        agent = {
            "kind": "codex",
            "state": "working",
            "foreground_cwd": "/tmp/project",
            "terminal_title_stripped": "Codex — project",
            "agent_session": {"agent": "codex", "kind": "id", "value": "sess-123"},
        }
        target = target_snapshot("w1:p2", agent)
        self.assertEqual(target["pane"], "w1:p2")
        self.assertEqual(target["session_id"], "sess-123")
        self.assertEqual(target["cwd"], "/tmp/project")
        self.assertEqual(target["title"], "Codex — project")

    def test_job_target_requires_same_pane_and_session(self):
        job = {"pane": "w1:p2", "agent_session": {"agent": "codex", "kind": "id", "value": "abc"}}
        session = {"agent": "codex", "kind": "id", "value": "abc"}
        self.assertTrue(job_matches_target(job, "w1:p2", session))
        self.assertFalse(job_matches_target(job, "w9:p9", session))
        self.assertFalse(job_matches_target(job, "w1:p2", None))

    def test_live_agent_snapshot_uses_pane_session_fallback(self):
        calls: list[list[str]] = []
        session = {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "sess-pane"}

        def fake_run(_herdr: str, _socket: str | None, args: list[str], timeout: float = 10.0):
            del timeout
            calls.append(list(args))
            if args[:2] == ["agent", "get"]:
                payload = {"result": {"agent": {
                    "kind": "codex", "state": "idle", "terminal_title_stripped": "Thread name",
                }}}
            elif args[:2] == ["pane", "get"]:
                payload = {"result": {"pane": {
                    "pane_id": "w1:p2", "agent_session": session, "foreground_cwd": "/tmp/project",
                }}}
            else:
                raise AssertionError(args)
            return subprocess.CompletedProcess(["herdr", *args], 0, json.dumps(payload), "")

        agent = live_agent_snapshot("herdr", None, "w1:p2", runner=fake_run)
        self.assertEqual(agent["agent_session"], session)
        self.assertEqual(agent["foreground_cwd"], "/tmp/project")
        self.assertEqual(calls, [["agent", "get", "w1:p2"], ["pane", "get", "w1:p2"]])

    def test_live_agent_snapshot_can_use_pane_session_when_agent_get_fails(self):
        session = {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "sess-pane"}

        def fake_run(_herdr: str, _socket: str | None, args: list[str], timeout: float = 10.0):
            del timeout
            if args[:2] == ["agent", "get"]:
                return subprocess.CompletedProcess(
                    ["herdr", *args], 1, "", '{"error":{"code":"agent_not_found"}}',
                )
            payload = {"result": {"pane": {
                "pane_id": "w1:p2", "agent_session": session, "agent_status": "unknown",
                "foreground_cwd": "/tmp/project", "terminal_title_stripped": "Codex thread",
            }}}
            return subprocess.CompletedProcess(["herdr", *args], 0, json.dumps(payload), "")

        agent = live_agent_snapshot("herdr", None, "w1:p2", runner=fake_run)
        self.assertTrue(is_codex(agent))
        self.assertEqual(agent["agent_session"], session)
        self.assertEqual(agent["state"], "unknown")
        self.assertEqual(agent["terminal_title_stripped"], "Codex thread")

    def test_claude_snapshot_uses_official_pane_session_fallback(self):
        session = {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "claude-session"}

        def fake_run(_herdr, _socket, args, timeout=10.0):
            if args[:2] == ["agent", "get"]:
                payload = {"result": {"agent": {"agent": "claude", "agent_status": "idle"}}}
            else:
                payload = {"result": {"pane": {"agent_session": session, "foreground_cwd": "/tmp/project"}}}
            return subprocess.CompletedProcess(["herdr", *args], 0, json.dumps(payload), "")

        agent = live_agent_snapshot("herdr", None, "w1:p2", runner=fake_run)
        self.assertEqual(target_snapshot("w1:p2", agent)["agent"], "claude")
        self.assertEqual(agent["agent_session"], session)


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(self.state)})
        self.env.start()
        self.agent = {
            "kind": "codex",
            "foreground_cwd": "/tmp/project",
            "agent_session": {"agent": "codex", "kind": "id", "value": "same"},
        }

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_create_stores_target_metadata(self):
        job = create_job(NOW + dt.timedelta(hours=1), "Continue", "w1:p2", "herdr", None, self.agent, "1h")
        self.assertEqual(job["target"]["session_id"], "same")
        self.assertEqual(job["target"]["cwd"], "/tmp/project")
        self.assertEqual(job["schedule_spec"], "1h")
        self.assertEqual(job["number"], 1)

    def test_claude_job_persists_identity_across_edit(self):
        agent = {"agent": "claude", "agent_status": "idle", "foreground_cwd": "/tmp/project",
                 "agent_session": {"agent": "claude", "kind": "id", "value": "claude-session"}}
        job = create_job(NOW + dt.timedelta(hours=1), "Continue", "w1:p2", "herdr", None, agent, "1h")
        self.assertTrue(update_job(job["id"], prompt="Run tests"))
        saved = read_jobs()[0]
        self.assertEqual(saved["target"], job["target"])
        self.assertEqual(saved["target"]["agent"], "claude")
        self.assertEqual(saved["agent_session"], agent["agent_session"])

    def test_unsupported_target_cannot_create_job(self):
        with self.assertRaises(ValueError):
            create_job(NOW + dt.timedelta(hours=1), "Continue", "w1:p2", "herdr", None, {"agent": "gemini"})
        self.assertEqual(read_jobs(), [])

    def test_edit_pending_job(self):
        job = create_job(NOW + dt.timedelta(hours=1), "Continue", "w1:p2", "herdr", None, self.agent, "1h")
        with locked_jobs() as jobs:
            jobs[0]["delivery_state"] = "waiting_for_ready"
            jobs[0]["delivery_note"] = "old waiting state"
        new_due = NOW + dt.timedelta(hours=3)
        self.assertTrue(update_job(job["id"], when=new_due, prompt="Retry", schedule_spec="3h"))
        current = read_jobs()[0]
        self.assertEqual(current["prompt"], "Retry")
        self.assertEqual(current["due_at"], new_due.isoformat())
        self.assertIsNone(current["delivery_state"])
        self.assertIsNone(current["delivery_note"])

    def test_cancel_preserves_history(self):
        job = create_job(NOW + dt.timedelta(hours=1), "Continue", "w1:p2", "herdr", None, self.agent)
        with locked_jobs() as jobs:
            jobs[0]["delivery_state"] = "waiting_for_ready"
            jobs[0]["delivery_note"] = "old waiting state"
        self.assertTrue(cancel_job(job["id"]))
        current = read_jobs()[0]
        self.assertEqual(current["status"], "cancelled")
        self.assertIsNone(current["delivery_state"])
        self.assertIsNone(current["delivery_note"])

    def test_delete_removes_record(self):
        job = create_job(NOW + dt.timedelta(hours=1), "Continue", "w1:p2", "herdr", None, self.agent)
        self.assertTrue(delete_job(job["id"]))
        self.assertEqual(read_jobs(), [])

    def test_job_numbers_are_monotonic_even_after_delete(self):
        first = create_job(NOW + dt.timedelta(hours=1), "A", "w1:p2", "herdr", None, self.agent)
        second = create_job(NOW + dt.timedelta(hours=2), "B", "w1:p2", "herdr", None, self.agent)
        self.assertTrue(delete_job(second["id"]))
        third = create_job(NOW + dt.timedelta(hours=3), "C", "w1:p2", "herdr", None, self.agent)
        self.assertEqual([first["number"], second["number"], third["number"]], [1, 2, 3])

    def test_finished_history_storage_is_bounded_but_pending_is_preserved(self):
        stamp = NOW.isoformat()
        jobs = []
        for number in range(1, 61):
            jobs.append({
                "id": f"done{number}", "number": number, "status": "sent",
                "created_at": stamp, "updated_at": (NOW + dt.timedelta(minutes=number)).isoformat(),
                "sent_at": (NOW + dt.timedelta(minutes=number)).isoformat(), "due_at": stamp,
                "prompt": str(number), "pane": "w1:p2",
            })
        jobs.append({
            "id": "pending", "number": 61, "status": "pending", "created_at": stamp,
            "updated_at": stamp, "due_at": (NOW + dt.timedelta(days=1)).isoformat(),
            "prompt": "pending", "pane": "w1:p2",
        })
        (self.state / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
        current = read_jobs()
        finished = [job for job in current if job["status"] != "pending"]
        self.assertEqual(len(finished), 50)
        self.assertTrue(any(job["id"] == "pending" for job in current))
        self.assertFalse(any(job["id"] == "done1" for job in current))
        self.assertTrue(any(job["id"] == "done60" for job in current))

    def test_legacy_job_is_migrated_on_read(self):
        legacy = [{
            "id": "legacy01", "created_at": NOW.isoformat(),
            "due_at": (NOW + dt.timedelta(hours=1)).isoformat(), "prompt": "Continue",
            "pane": "w1:p2", "agent_session": self.agent["agent_session"], "status": "pending",
        }]
        (self.state / "jobs.json").write_text(json.dumps(legacy), encoding="utf-8")
        job = read_jobs()[0]
        self.assertEqual(job["target"]["session_id"], "same")
        self.assertIn("updated_at", job)
        self.assertEqual(job["number"], 1)

    def test_jobs_without_agent_identity_migrate_to_codex(self):
        legacy = [{"id": "legacy", "pane": "w1:p2", "status": "pending", "target": {"cwd": "/tmp/project"}}]
        (self.state / "jobs.json").write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(read_jobs()[0]["target"]["agent"], "codex")


class HerdrVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(self.state)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_schedule_metadata_reports_and_clears_display_token(self):
        pending = [{
            "id": "one",
            "number": 1,
            "status": "pending",
            "pane": "w1:p2",
            "due_at": (dt.datetime.now().astimezone() + dt.timedelta(hours=2)).isoformat(),
        }]
        calls = []

        def fake_run(_herdr, _socket, args, timeout=10.0):
            calls.append((args, timeout))
            return subprocess.CompletedProcess(["herdr", *args], 0, "{}", "")

        with mock.patch("scheduler_core.run_herdr", side_effect=fake_run):
            sync_schedule_metadata(pending, "herdr", "/tmp/herdr.sock")
            sync_schedule_metadata([], "herdr", "/tmp/herdr.sock")

        report = calls[0][0]
        clear = calls[1][0]
        self.assertEqual(report[:5], [
            "pane", "report-metadata", "w1:p2", "--source", "plugin:codex.scheduler",
        ])
        self.assertEqual(report[5], "--token")
        self.assertTrue(report[6].startswith("scheduler=⏱ 1 scheduled · next in "))
        self.assertEqual(clear, [
            "pane", "report-metadata", "w1:p2", "--source", "plugin:codex.scheduler",
            "--clear-token", "scheduler",
        ])

    def test_notification_uses_supported_cli_shape(self):
        with mock.patch("scheduler_core.run_herdr") as run:
            run.return_value = subprocess.CompletedProcess(["herdr"], 0, "{}", "")
            self.assertTrue(show_notification("herdr", None, "Scheduled", "#1 · Continue", sound="done"))
        args = run.call_args.args[2]
        self.assertEqual(args, [
            "notification", "show", "Scheduled", "--body", "#1 · Continue", "--sound", "done",
        ])


class LineBufferTests(unittest.TestCase):
    def test_backspace(self):
        b = manager_ui.LineBuffer.from_text("abc")
        b.backspace()
        self.assertEqual(b.value, "ab")
        self.assertEqual(b.cursor, 2)

    def test_delete_midline(self):
        b = manager_ui.LineBuffer.from_text("abc")
        b.left()
        b.delete()
        self.assertEqual(b.value, "ab")

    def test_insert_midline(self):
        b = manager_ui.LineBuffer.from_text("ac")
        b.left()
        b.insert("b")
        self.assertEqual(b.value, "abc")

    def test_ctrl_u_model_clear(self):
        b = manager_ui.LineBuffer.from_text("abc")
        b.clear()
        self.assertEqual(b.value, "")

    def test_erase_word(self):
        b = manager_ui.LineBuffer.from_text("hello world")
        b.erase_word()
        b.insert("there")
        self.assertEqual(b.value, "hello there")

    def test_unicode(self):
        b = manager_ui.LineBuffer.from_text("Привет")
        b.backspace()
        self.assertEqual(b.value, "Приве")

    def test_long_viewport_keeps_cursor_visible(self):
        b = manager_ui.LineBuffer.from_text("abcdefghijklmnopqrstuvwxyz")
        visible, cursor = b.viewport(8)
        self.assertLessEqual(manager_ui._cell_width(visible), 8)
        self.assertLessEqual(cursor, 8)
        self.assertTrue(visible.startswith("…"))

    def test_empty_viewport_uses_placeholder(self):
        b = manager_ui.LineBuffer.empty()
        visible, cursor = b.viewport(20, "[Continue]")
        self.assertEqual(visible, "[Continue]")
        self.assertEqual(cursor, 0)

    def test_line_break_is_stored_as_newline_and_shown_as_glyph(self):
        b = manager_ui.LineBuffer.from_text("first")
        b.insert_line_break()
        b.insert("second")
        self.assertEqual(b.value, "first\nsecond")
        visible, cursor = b.viewport(40)
        self.assertEqual(visible, "first" + manager_ui.LINE_BREAK_GLYPH + "second")
        self.assertEqual(cursor, len(visible))

    def test_cursor_stays_correct_after_line_break_in_middle(self):
        b = manager_ui.LineBuffer.from_text("ab")
        b.left()
        b.insert_line_break()
        self.assertEqual(b.value, "a\nb")
        _visible, cursor = b.viewport(40)
        self.assertEqual(cursor, 2)


class FakeWindow:
    def __init__(self, keys: list[Any] | None = None, rows: int = 36, cols: int = 100):
        self.keys = list(keys or [])
        self.rows = rows
        self.cols = cols
        self.timeout_value = -1
        self.cursor = (0, 0)
        self.draws: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return self.rows, self.cols

    def addstr(self, *args):
        if len(args) == 1:
            text = args[0]
            y, x = self.cursor
        elif len(args) >= 3:
            y, x, text = args[:3]
        else:
            raise AssertionError(args)
        self.draws.append((int(y), int(x), str(text)))
        self.cursor = (int(y), int(x) + len(str(text)))

    def move(self, y, x):
        self.cursor = (int(y), int(x))

    def refresh(self):
        pass

    def erase(self):
        self.draws.append((-1, -1, "<erase>"))

    def keypad(self, _value):
        pass

    def timeout(self, value):
        self.timeout_value = value

    def get_wch(self):
        if not self.keys:
            raise curses.error("no input")
        return self.keys.pop(0)


class CursesInputTests(unittest.TestCase):
    def test_typing_and_backspace_stays_on_one_row(self):
        win = FakeWindow(["a", "b", "c", curses.KEY_BACKSPACE, "d", "\n"])
        value = manager_ui.read_field(win, 5, "Prompt: ")
        self.assertEqual(value, "abd")
        self.assertTrue(all(y in (-1, 5) for y, _x, _text in win.draws))

    def test_delete_and_arrow_edit(self):
        win = FakeWindow(["a", "b", "c", curses.KEY_LEFT, curses.KEY_DC, "\n"])
        self.assertEqual(manager_ui.read_field(win, 5, "Prompt: "), "ab")

    def test_home_end_and_midline_insert(self):
        win = FakeWindow(["b", "c", curses.KEY_HOME, "a", curses.KEY_END, "d", "\n"])
        self.assertEqual(manager_ui.read_field(win, 5, "Prompt: "), "abcd")

    def test_default_is_placeholder_not_prefilled(self):
        win = FakeWindow(["x", "\n"])
        self.assertEqual(manager_ui.read_field(win, 5, "Prompt: ", "Continue"), "x")

    def test_enter_accepts_default(self):
        win = FakeWindow(["\n"])
        self.assertEqual(manager_ui.read_field(win, 5, "Prompt: ", "Continue"), "Continue")

    def test_prefill_can_be_backspaced(self):
        win = FakeWindow([curses.KEY_BACKSPACE, "!", "\n"])
        self.assertEqual(manager_ui.read_field(win, 5, "Prompt: ", "Continue", prefill=True), "Continu!")

    def test_ctrl_u_and_ctrl_w(self):
        win = FakeWindow(["h", "i", " ", "x", "\x17", "y", "\x15", "z", "\n"])
        self.assertEqual(manager_ui.read_field(win, 5, "Prompt: "), "z")

    def test_single_escape_cancels(self):
        win = FakeWindow(["\x1b"])
        with self.assertRaises(manager_ui.EscapePressed):
            manager_ui.read_field(win, 5, "Prompt: ")

    def test_escape_is_not_timing_sensitive(self):
        win = FakeWindow(["\x1b", "\x1b"])
        with self.assertRaises(manager_ui.EscapePressed):
            manager_ui.read_field(win, 5, "Prompt: ")
        self.assertEqual(win.keys, ["\x1b"])


class ManagerDisplayTests(unittest.TestCase):
    def test_claude_target_and_details_show_correct_agent(self):
        agent = {"agent": "claude", "agent_status": "idle", "foreground_cwd": "/tmp/project"}
        self.assertIn("Claude Code idle", manager_ui._target_lines("w1:p1", agent, None, 100)[0])
        self.assertTrue(manager_ui._button_enabled("New", None, "w1:p1", agent))
        job = {"pane": "w1:p1", "target": {"agent": "claude"}}
        self.assertIn("Claude Code", "\n".join(line.text for line in manager_ui._details_lines(job, 100)))

    def test_codex_job_is_other_target_in_claude_pane_without_session_id(self):
        job = {"pane": "w1:p1", "target": {"agent": "codex"}, "agent_session": None}
        self.assertFalse(manager_ui.job_matches_current(job, "w1:p1", {"agent": "claude"}))
        self.assertTrue(manager_ui.job_matches_current(job, "w1:p1", {"agent": "codex"}))

    def test_resolve_target_accepts_claude_from_herdr(self):
        agent = {"agent": "claude", "agent_status": "idle"}
        with mock.patch.object(manager_ui, "target_pane", return_value="w1:p1"), \
                mock.patch.object(manager_ui, "live_agent_snapshot", return_value=agent):
            self.assertEqual(manager_ui.resolve_target(), ("w1:p1", agent, None))

    def test_history_is_limited_to_five_rows(self):
        jobs = []
        base = dt.datetime.now().astimezone()
        for number in range(1, 8):
            stamp = (base + dt.timedelta(minutes=number)).isoformat()
            jobs.append({
                "id": f"job{number}", "number": number, "status": "sent",
                "created_at": stamp, "updated_at": stamp, "sent_at": stamp,
                "due_at": stamp, "prompt": f"history-prompt-{number}", "pane": "w1:p1",
            })
        lines = manager_ui._manager_lines(jobs, None, None, None, 100, 80)
        text = "\n".join(lines)
        self.assertIn("Recent · last 5", text)
        self.assertEqual(text.count("history-prompt-"), 5)
        self.assertIn("history-prompt-7", text)
        self.assertNotIn("history-prompt-1", text)

    def test_current_rows_do_not_repeat_target(self):
        due = (dt.datetime.now().astimezone() + dt.timedelta(hours=2)).isoformat()
        agent = {"kind": "codex", "foreground_cwd": "/tmp/ff5m"}
        jobs = [{
            "id": "job1", "number": 1, "status": "pending", "due_at": due,
            "prompt": "Continue", "pane": "w5:p1", "agent_session": None,
            "target": {"pane": "w5:p1", "cwd": "/tmp/ff5m"},
        }]
        lines = manager_ui._manager_lines(jobs, "w5:p1", agent, None, 100, 80)
        scheduled = "\n".join(lines[lines.index("Scheduled"):])
        self.assertIn("Continue", scheduled)
        self.assertNotIn("w5:p1 · ff5m", scheduled)

    def test_layout_lines_fit_width(self):
        due = (dt.datetime.now().astimezone() + dt.timedelta(hours=2)).isoformat()
        jobs = [{
            "id": "job1", "number": 1, "status": "pending", "due_at": due,
            "prompt": "x" * 200, "pane": "w5:p1", "agent_session": None,
            "target": {"pane": "w5:p1", "cwd": "/tmp/very-long-project-name"},
        }]
        for width in (40, 60, 90):
            lines = manager_ui._manager_lines(jobs, "w5:p1", {"kind": "codex"}, None, width, 40)
            self.assertTrue(all(manager_ui._cell_width(line) <= width for line in lines))

    def test_action_buttons_expose_hotkeys(self):
        labels = [label for row in manager_ui._button_layout(120) for _index, label, _x1, _x2 in row]
        self.assertIn("[N] New", labels)
        self.assertIn("[D] Delete", labels)
        self.assertIn("[V] Details", labels)
        self.assertIn("[Q] Close", labels)

    def test_target_uses_live_codex_title_instead_of_missing_session_message(self):
        agent = {
            "kind": "codex",
            "state": "idle",
            "foreground_cwd": "/tmp/ff5m",
            "terminal_title_stripped": "Scheduler UX · ff5m",
        }
        lines = manager_ui._target_lines("w5:p1", agent, None, 100)
        self.assertIn("Thread   Scheduler UX · ff5m", lines[1])
        self.assertNotIn("Session", "\n".join(lines))
        self.assertNotIn("not reported", "\n".join(lines))

    def test_root_view_does_not_duplicate_herdr_popup_title(self):
        win = FakeWindow(rows=30, cols=100)
        state = manager_ui.MainState()
        agent = {
            "kind": "codex",
            "state": "idle",
            "foreground_cwd": "/tmp/ff5m",
            "terminal_title_stripped": "Scheduler UX · ff5m",
        }
        manager_ui._render_main(win, "w5:p1", agent, None, [], state)
        text = "\n".join(value for _y, _x, value in win.draws)
        self.assertNotIn(f"Scheduler {manager_ui.VERSION}", text)
        self.assertIn("Target", text)
        self.assertIn("[N]", text)
        self.assertIn(" New", text)

    def test_terminal_style_reduces_curses_escape_delay(self):
        with mock.patch.object(curses, "set_escdelay") as set_delay, \
                mock.patch.object(curses, "has_colors", return_value=False):
            manager_ui._init_terminal_style()
        set_delay.assert_called_once_with(manager_ui.ESCDELAY_MS)
        self.assertLessEqual(manager_ui.ESCDELAY_MS, 50)

    def test_scheduled_and_recent_share_one_table_header(self):
        now = dt.datetime.now().astimezone()
        jobs = [
            {
                "id": "pending", "number": 1, "status": "pending",
                "due_at": (now + dt.timedelta(hours=2)).isoformat(), "prompt": "Continue",
                "pane": "w1:p1", "agent_session": None, "target": {"pane": "w1:p1"},
            },
            {
                "id": "sent", "number": 2, "status": "sent",
                "created_at": now.isoformat(), "updated_at": now.isoformat(),
                "sent_at": now.isoformat(), "due_at": now.isoformat(), "prompt": "Run tests",
                "pane": "w1:p1", "agent_session": None, "target": {"pane": "w1:p1"},
            },
        ]
        lines = manager_ui._manager_lines(jobs, "w1:p1", {"kind": "codex"}, None, 90, 40)
        header = lines[0]
        self.assertIn("ID", header)
        self.assertIn("When", header)
        self.assertIn("In", header)
        self.assertIn("Status", header)
        self.assertIn("Prompt", header)
        self.assertIn("Target", header)
        self.assertEqual(sum("ID" in line and "When" in line and "Status" in line for line in lines), 1)
        self.assertIn("Scheduled", lines)
        self.assertIn("Recent · last 5", lines)

    def test_details_wrap_full_prompt_and_use_sections(self):
        now = dt.datetime.now().astimezone()
        prompt = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
        job = {
            "id": "job", "number": 3, "status": "pending",
            "created_at": now.isoformat(), "updated_at": now.isoformat(),
            "due_at": (now + dt.timedelta(hours=1)).isoformat(), "prompt": prompt,
            "pane": "w1:p1", "attempts": 0,
            "agent_session": {"agent": "codex", "kind": "id", "value": "sess-123"},
            "target": {
                "pane": "w1:p1", "agent": "codex", "cwd": "/tmp/project",
                "title": "Readable thread", "session_id": "sess-123",
            },
        }
        lines = manager_ui._details_lines(job, 42)
        text = "\n".join(line.text for line in lines)
        self.assertIn("Schedule", text)
        self.assertIn("Prompt", text)
        self.assertIn("Target", text)
        self.assertIn("Delivery", text)
        self.assertIn("Metadata", text)
        self.assertIn("Session ID   sess-123", text)
        self.assertNotIn("…", text)
        normalized = " ".join(text.split())
        self.assertIn(prompt, normalized)
        self.assertNotIn(now.isoformat(), text)

    def test_details_explain_missing_session_instead_of_dash(self):
        job = {
            "id": "job", "number": 1, "status": "pending", "due_at": NOW.isoformat(),
            "prompt": "Continue", "pane": "w1:p1", "target": {"pane": "w1:p1"},
        }
        text = "\n".join(line.text for line in manager_ui._details_lines(job, 80))
        self.assertIn("Not captured / not reported by Herdr", text)


class ManagerFlowTests(unittest.TestCase):
    def test_claude_create_and_edit_from_codex_pane_keep_claude_target(self):
        claude = {"agent": "claude", "agent_status": "idle", "foreground_cwd": "/tmp/project",
                  "agent_session": {"agent": "claude", "kind": "id", "value": "claude-session"}}
        codex = {"agent": "codex", "agent_status": "idle"}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": td}), \
                mock.patch.object(manager_ui, "ensure_daemon"), \
                mock.patch.object(manager_ui, "_refresh_metadata"), \
                mock.patch.object(manager_ui, "_notify") as notify, \
                mock.patch.object(curses, "curs_set", side_effect=curses.error):
            win = FakeWindow(["\n", "\t", "\t", "\n"], rows=36, cols=100)
            _banner, job_id = manager_ui._schedule_form(win, "w1:p1", claude)
            job = read_jobs()[0]
            self.assertEqual(job_id, job["id"])
            self.assertEqual(job["target"]["agent"], "claude")
            notify.assert_called_with("Claude Code prompt scheduled", mock.ANY)

            win = FakeWindow(["\n", "\x15", *list("Run tests"), "\t", "\t", "\n"], rows=36, cols=100)
            manager_ui._schedule_form(win, "w2:p1", codex, job)
            saved = read_jobs()[0]
            self.assertEqual(saved["prompt"], "Run tests")
            self.assertEqual(saved["target"], job["target"])
            self.assertEqual(saved["pane"], "w1:p1")
            notify.assert_called_with("Claude Code schedule updated", mock.ANY)

    def test_multiline_paste_in_prompt_does_not_submit_the_form(self):
        codex = {"agent": "codex", "agent_status": "idle", "foreground_cwd": "/tmp/project",
                 "agent_session": {"agent": "codex", "kind": "id", "value": "codex-session"}}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": td}), \
                mock.patch.object(manager_ui, "ensure_daemon"), \
                mock.patch.object(manager_ui, "_refresh_metadata"), \
                mock.patch.object(manager_ui, "_notify"), \
                mock.patch.object(curses, "curs_set", side_effect=curses.error):
            # Pasted text arrives as characters and Enter keys; the Enter keys
            # between lines must stay inside the prompt.
            pasted = ["Fix", "\r", "the", "\n", "tests"]
            win = FakeWindow(["\n", *pasted, "\t", "\t", "\n"], rows=36, cols=100)
            manager_ui._schedule_form(win, "w1:p1", codex)
            job = read_jobs()[0]
            self.assertEqual(job["prompt"], "Fix\nthe\ntests")

    def test_create_edit_cancel_delete_without_job_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            state.mkdir()
            agent = {
                "kind": "codex", "state": "idle", "foreground_cwd": "/tmp/project",
                "agent_session": {"agent": "codex", "kind": "id", "value": "sess"},
            }
            # New: replace default 15h with 1h, accept Continue, Save.
            keys: list[Any] = ["n", "\x15", "1", "h", "\n", "\t", "\t", "\n"]
            # Edit selected job: replace When with 30m, replace prompt with Retry, Save.
            keys += ["e", "\x15", "3", "0", "m", "\n", "\x15"] + list("Retry") + ["\t", "\t", "\n"]
            # Cancel and Delete both use selectable No/Yes dialogs, defaulting to No.
            keys += ["c", curses.KEY_RIGHT, "\n", "d", curses.KEY_RIGHT, "\n", "\x1b"]
            win = FakeWindow(keys, rows=40, cols=110)

            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(state)}), \
                    mock.patch.object(manager_ui, "resolve_target", return_value=("w1:p1", agent, None)), \
                    mock.patch.object(manager_ui, "ensure_daemon", return_value=None), \
                    mock.patch.object(manager_ui, "_notify", return_value=None), \
                    mock.patch.object(manager_ui, "_refresh_metadata", return_value=None), \
                    mock.patch.object(curses, "curs_set", side_effect=curses.error):
                rc = manager_ui._run_manager(win)
                self.assertEqual(rc, 0)
                self.assertEqual(read_jobs(), [])

    def test_visible_q_hotkey_closes_manager(self):
        agent = {"kind": "codex", "state": "idle", "foreground_cwd": "/tmp/project"}
        with tempfile.TemporaryDirectory() as td:
            win = FakeWindow(["q"], rows=36, cols=100)
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": td}), \
                    mock.patch.object(manager_ui, "resolve_target", return_value=("w1:p1", agent, None)), \
                    mock.patch.object(manager_ui, "ensure_daemon", return_value=None), \
                    mock.patch.object(manager_ui, "_refresh_metadata", return_value=None), \
                    mock.patch.object(curses, "curs_set", side_effect=curses.error):
                self.assertEqual(manager_ui._run_manager(win), 0)

    def test_two_ordinary_escapes_from_form_return_to_codex(self):
        agent = {"kind": "codex", "state": "idle", "foreground_cwd": "/tmp/project"}
        state_tmp = tempfile.TemporaryDirectory()
        try:
            state = Path(state_tmp.name)
            win = FakeWindow(["n", "\x1b", "\x1b"], rows=36, cols=100)
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(state)}), \
                    mock.patch.object(manager_ui, "resolve_target", return_value=("w1:p1", agent, None)), \
                    mock.patch.object(manager_ui, "ensure_daemon", return_value=None), \
                    mock.patch.object(manager_ui, "_refresh_metadata", return_value=None), \
                    mock.patch.object(curses, "curs_set", side_effect=curses.error):
                self.assertEqual(manager_ui._run_manager(win), 0)
        finally:
            state_tmp.cleanup()


class FakeHerdrDeliveryTests(unittest.TestCase):
    def _run_due(self, current_agent: dict, expected_session: dict | None,
                 expected_cwd: str | None = None, expected_kind: str = "codex") -> tuple[list[dict], list[list[str]]]:
        from scheduler_core import locked_jobs, local_now

        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            state.mkdir()
            due = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).isoformat()
            target = {"pane": "w1:p2", "agent": expected_kind,
                      "session_id": None, "session": expected_session, "cwd": expected_cwd}
            if expected_session:
                target["session_id"] = expected_session.get("value")
            (state / "jobs.json").write_text(json.dumps([{
                "id": "deadbeef", "number": 1, "created_at": due, "updated_at": due, "due_at": due,
                "prompt": "Continue", "pane": "w1:p2", "herdr": "herdr", "socket_path": None,
                "agent_session": expected_session, "target": target, "status": "pending", "attempts": 0,
                "last_attempt_at": None, "last_error": None, "sent_at": None,
            }]), encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(_herdr: str, _socket: str | None, args: list[str], timeout: float = 10.0):
                del timeout
                calls.append(list(args))
                if args[:2] == ["agent", "get"]:
                    payload = json.dumps({"result": {"agent": current_agent}})
                    return subprocess.CompletedProcess(["herdr", *args], 0, payload, "")
                if args[:2] == ["agent", "prompt"]:
                    payload = json.dumps({"result": {"type": "agent_prompted"}})
                    return subprocess.CompletedProcess(["herdr", *args], 0, payload, "")
                return subprocess.CompletedProcess(["herdr", *args], 1, "", "unexpected")

            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(state)}), \
                    mock.patch.object(daemon, "run_herdr", side_effect=fake_run):
                with locked_jobs() as jobs:
                    daemon.process_due_job(jobs[0], local_now())
            return json.loads((state / "jobs.json").read_text()), calls

    def test_claude_due_job_delivers_via_same_herdr_prompt(self):
        session = {"agent": "claude", "kind": "id", "value": "same"}
        for state in ("idle", "done"):
            with self.subTest(state=state):
                agent = {"agent": "claude", "agent_status": state, "agent_session": session}
                with mock.patch.object(daemon, "notify_job") as notify:
                    jobs, calls = self._run_due(agent, session, expected_kind="claude")
                self.assertEqual(jobs[0]["status"], "sent")
                self.assertEqual(calls.count(["agent", "prompt", "w1:p2", "Continue"]), 1)
                notify.assert_called_with(jobs[0], "Claude Code prompt delivered", "#1 · Continue", "done")

    def test_claude_busy_or_unknown_never_receives_prompt(self):
        session = {"agent": "claude", "kind": "id", "value": "same"}
        for state in ("working", "blocked", "unknown", None):
            with self.subTest(state=state):
                agent = {"agent": "claude", "agent_status": state, "agent_session": session}
                jobs, calls = self._run_due(agent, session, expected_kind="claude")
                self.assertEqual(jobs[0]["status"], "pending")
                self.assertFalse(self._prompt_called(calls))
                if state in ("working", "blocked"):
                    self.assertIn("Claude Code is", jobs[0]["delivery_note"])

    def test_claude_replaced_session_is_stale(self):
        expected = {"agent": "claude", "kind": "id", "value": "same"}
        actual = {"agent": "claude", "kind": "id", "value": "new"}
        agent = {"agent": "claude", "agent_status": "idle", "agent_session": actual}
        jobs, calls = self._run_due(agent, expected, expected_kind="claude")
        self.assertEqual(jobs[0]["status"], "stale")
        self.assertIn("Claude Code session", jobs[0]["last_error"])
        self.assertFalse(self._prompt_called(calls))

    def test_claude_project_fallback_checks_cwd(self):
        for cwd, status in (("/tmp/project", "sent"), ("/tmp/other", "stale"), (None, "pending")):
            with self.subTest(cwd=cwd):
                agent = {"agent": "claude", "agent_status": "idle", "foreground_cwd": cwd}
                jobs, calls = self._run_due(agent, None, "/tmp/project", expected_kind="claude")
                self.assertEqual(jobs[0]["status"], status)
                self.assertEqual(self._prompt_called(calls), status == "sent")

    def test_agent_replacement_without_session_is_stale_in_both_directions(self):
        for expected_kind, actual_kind in (("codex", "claude"), ("claude", "codex"), ("claude", "gemini")):
            with self.subTest(expected=expected_kind, actual=actual_kind):
                agent = {"agent": actual_kind, "agent_status": "idle", "foreground_cwd": "/tmp/project"}
                jobs, calls = self._run_due(agent, None, "/tmp/project", expected_kind=expected_kind)
                self.assertEqual(jobs[0]["status"], "stale")
                self.assertFalse(self._prompt_called(calls))

    @staticmethod
    def _prompt_called(calls: list[list[str]]) -> bool:
        return any(args[:2] == ["agent", "prompt"] for args in calls)

    def test_due_job_prompts_same_session(self):
        session = {"agent": "codex", "kind": "id", "value": "same"}
        jobs, calls = self._run_due({"kind": "codex", "state": "idle", "agent_session": session}, session)
        self.assertEqual(jobs[0]["status"], "sent")
        self.assertTrue(self._prompt_called(calls))

    def test_due_job_waits_while_codex_is_working(self):
        session = {"agent": "codex", "kind": "id", "value": "same"}
        current = {"kind": "codex", "state": "working", "agent_session": session}
        jobs, calls = self._run_due(current, session)
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertEqual(jobs[0]["delivery_state"], "waiting_for_ready")
        self.assertFalse(self._prompt_called(calls))

    def test_due_job_waits_while_codex_is_blocked(self):
        session = {"agent": "codex", "kind": "id", "value": "same"}
        current = {"kind": "codex", "state": "blocked", "agent_session": session}
        jobs, calls = self._run_due(current, session)
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertFalse(self._prompt_called(calls))

    def test_changed_session_is_stale(self):
        expected = {"agent": "codex", "kind": "id", "value": "same"}
        actual = {"agent": "codex", "kind": "id", "value": "different"}
        jobs, calls = self._run_due({"kind": "codex", "agent_session": actual}, expected)
        self.assertEqual(jobs[0]["status"], "stale")
        self.assertIsNotNone(jobs[0].get("updated_at"))
        self.assertFalse(self._prompt_called(calls))

    def test_without_session_same_project_is_allowed(self):
        current = {"kind": "codex", "state": "idle", "foreground_cwd": "/tmp/project"}
        jobs, calls = self._run_due(current, None, "/tmp/project")
        self.assertEqual(jobs[0]["status"], "sent")
        self.assertTrue(self._prompt_called(calls))

    def test_without_session_changed_project_is_stale(self):
        jobs, calls = self._run_due({"kind": "codex", "foreground_cwd": "/tmp/other"}, None, "/tmp/project")
        self.assertEqual(jobs[0]["status"], "stale")
        self.assertFalse(self._prompt_called(calls))

    def test_without_session_missing_current_project_retries(self):
        jobs, calls = self._run_due({"kind": "codex"}, None, "/tmp/project")
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertEqual(jobs[0]["attempts"], 1)
        self.assertFalse(self._prompt_called(calls))


class RealPtyCursesSmokeTest(unittest.TestCase):
    def test_backspace_flow_in_real_curses_terminal(self):
        self._run_backspace_flow("codex")

    def test_claude_schedule_in_real_curses_terminal(self):
        self._run_backspace_flow("claude")

    def _run_backspace_flow(self, kind: str):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state = tmp / "state"
            state.mkdir()
            fake = tmp / "herdr"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if sys.argv[1:3] == ['agent','get']:\n"
                f" agent={{'agent':{kind!r},'agent_status':'idle','foreground_cwd':'/tmp/project'}}\n"
                f" agent['agent_session']={{'agent':{kind!r},'kind':'id','value':'sess'}}\n"
                " print(json.dumps({'result':{'agent':agent}}))\n"
                " raise SystemExit(0)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

            master, slave = pty.openpty()
            env = os.environ.copy()
            env.update({
                "TERM": "xterm-256color",
                "PYTHONPATH": str(SCRIPTS),
                "HERDR_BIN_PATH": str(fake),
                "HERDR_ACTIVE_PANE_ID": "w1:p1",
                "HERDR_PLUGIN_STATE_DIR": str(state),
            })
            body = "import manager_ui; manager_ui.ensure_daemon=lambda:None; raise SystemExit(manager_ui.main())"
            proc = subprocess.Popen(
                [sys.executable, "-c", body], stdin=slave, stdout=slave, stderr=slave,
                env=env, close_fds=True,
            )
            os.close(slave)
            output = bytearray()

            def wait_for(token: bytes, start: int = 0, timeout: float = 5.0) -> bool:
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if token in output[start:]:
                        return True
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if ready:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                return token in output[start:]

            try:
                self.assertTrue(wait_for(b"Target"))
                start = len(output)
                os.write(master, b"n")
                self.assertTrue(wait_for(b"New schedule", start))

                start = len(output)
                # Curses may only redraw changed cells; the Prompt label was
                # already rendered when the form opened and need not reappear.
                # Enter leaves the Prompt field as a line break, so Save is reached
                # with Tab presses before the final Enter.
                os.write(master, b"\x15" + b"15x\x7fh\r" + b"\t\t" + b"\r")
                self.assertTrue(wait_for(b"Scheduled for", start))

                os.write(master, b"\x1b")
                # Drain output while curses restores the terminal; a full PTY
                # buffer can otherwise block shutdown on macOS.
                deadline = time.time() + 5
                while proc.poll() is None and time.time() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if ready:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                proc.wait(timeout=1)
                self.assertEqual(proc.returncode, 0)

                jobs = json.loads((state / "jobs.json").read_text(encoding="utf-8"))
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["schedule_spec"], "15h")
                self.assertEqual(jobs[0]["prompt"], "Continue")
                self.assertEqual(jobs[0]["target"]["agent"], kind)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)


class AsciiPrefixConfigTests(unittest.TestCase):
    FLAG = "switch_ascii_input_source_in_prefix = true"

    def test_adds_flag_under_existing_experimental_section(self):
        text = '[keys]\nprefix = "ctrl+b"\n\n[experimental]\npane_history = false\n'
        out = install_keybind.ensure_ascii_prefix(text)
        self.assertIn(f"[experimental]\n{self.FLAG}\npane_history = false", out)

    def test_replaces_existing_false_value(self):
        text = "[experimental]\nswitch_ascii_input_source_in_prefix = false\n"
        out = install_keybind.ensure_ascii_prefix(text)
        self.assertEqual(out.count("switch_ascii_input_source_in_prefix"), 1)
        self.assertIn(self.FLAG, out)

    def test_leaves_commented_default_alone(self):
        text = "[experimental]\n# switch_ascii_input_source_in_prefix = false\n"
        out = install_keybind.ensure_ascii_prefix(text)
        self.assertIn(f"[experimental]\n{self.FLAG}\n# switch_ascii_input_source_in_prefix = false", out)

    def test_creates_section_when_missing(self):
        out = install_keybind.ensure_ascii_prefix('[ui]\nx = 1\n')
        self.assertEqual(out, '[ui]\nx = 1\n\n[experimental]\n' + self.FLAG + "\n")

    def test_empty_config_gets_only_the_section(self):
        self.assertEqual(install_keybind.ensure_ascii_prefix(""), "[experimental]\n" + self.FLAG + "\n")


class PackagingAndKeybindingTests(unittest.TestCase):
    def test_keybinding_uses_managed_plugin_popup_action(self):
        block = install_keybind.block(ROOT)
        self.assertIn('type = "plugin_action"', block)
        self.assertIn('command = "codex.scheduler.manage"', block)
        self.assertNotIn('type = "popup"', block)
        self.assertNotIn("manager_ui.py", block)
        self.assertIn('key = "prefix+s"', block)

    def test_manifest_and_installer_versions_match(self):
        manifest = (ROOT / "herdr-plugin.toml").read_text(encoding="utf-8")
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn(f'version = "{manager_ui.VERSION}"', manifest)
        self.assertIn(f"Installed Codex & Claude Scheduler {manager_ui.VERSION}", installer)
        self.assertIn("for agent in codex claude", installer)
        self.assertNotIn("0.2.4", manifest)
        self.assertNotIn("0.2.4", installer)

    def test_open_manager_action_preserves_target_without_target_pane(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            calls = tmp / "calls.log"
            fake = tmp / "herdr"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$CALLS"\n', encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env.update({"HERDR_BIN_PATH": str(fake), "HERDR_PANE_ID": "w1:p2", "CALLS": str(calls)})
            subprocess.run(["bash", str(SCRIPTS / "open-manager.sh")], env=env, check=True)
            args = calls.read_text()
            self.assertIn("--placement popup", args)
            self.assertIn("CODEX_SCHED_TARGET_PANE=w1:p2", args)
            self.assertNotIn("--target-pane", args)

    def test_installer_is_idempotent_and_uninstall_preserves_other_config(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            config = home / ".config" / "herdr" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text("[ui]\nsidebar = true\n", encoding="utf-8")
            fake = tmp / "herdr"
            fake.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then echo \"herdr 0.9.0\"; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env.update({"HOME": str(home), "HERDR_BIN_PATH": str(fake)})

            subprocess.run(["bash", str(ROOT / "install.sh")], env=env, check=True, capture_output=True, text=True)
            subprocess.run(["bash", str(ROOT / "install.sh")], env=env, check=True, capture_output=True, text=True)
            text = config.read_text(encoding="utf-8")
            self.assertEqual(text.count(install_keybind.BEGIN), 1)
            self.assertIn("[ui]", text)
            self.assertIn('key = "prefix+s"', text)

            subprocess.run(["bash", str(ROOT / "uninstall.sh")], env=env, check=True, capture_output=True, text=True)
            text = config.read_text(encoding="utf-8")
            self.assertNotIn(install_keybind.BEGIN, text)
            self.assertIn("[ui]", text)

    def test_shell_scripts_parse(self):
        for name in ("install.sh", "uninstall.sh", "scripts/open-manager.sh"):
            subprocess.run(["bash", "-n", str(ROOT / name)], check=True)

    def test_python_files_compile(self):
        for path in (ROOT / "scripts").glob("*.py"):
            subprocess.run([sys.executable, "-m", "py_compile", str(path)], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
