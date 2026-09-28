from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

HISTORY_STORAGE_LIMIT = 50
AGENT_NAMES = {"codex": "Codex", "claude": "Claude Code"}

RELATIVE_RE = re.compile(r"^(?:\+)?(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?$", re.I)
TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")
DATE_TIME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{1,2}:\d{2})$")
WEEKDAY_TIME_RE = re.compile(r"^(?P<weekday>[A-Za-z]+)\s+(?P<time>\d{1,2}:\d{2})$")
TOMORROW_RE = re.compile(r"^tomorrow\s+(?P<time>\d{1,2}:\d{2})$", re.I)

WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _parse_clock(value: str) -> tuple[int, int]:
    match = TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("time must be HH:MM")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise ValueError("invalid clock time")
    return hour, minute


def parse_when(spec: str, now: dt.datetime | None = None) -> dt.datetime:
    spec = spec.strip()
    if not spec:
        raise ValueError("schedule is empty")
    now = now or local_now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    match = RELATIVE_RE.fullmatch(spec)
    if match and any(match.group(name) for name in ("days", "hours", "minutes")):
        delta = dt.timedelta(
            days=int(match.group("days") or 0),
            hours=int(match.group("hours") or 0),
            minutes=int(match.group("minutes") or 0),
        )
        if delta <= dt.timedelta(0):
            raise ValueError("relative interval must be greater than zero")
        return now + delta

    if TIME_RE.fullmatch(spec):
        hour, minute = _parse_clock(spec)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        return candidate

    match = TOMORROW_RE.fullmatch(spec)
    if match:
        hour, minute = _parse_clock(match.group("time"))
        tomorrow = now.date() + dt.timedelta(days=1)
        return dt.datetime.combine(tomorrow, dt.time(hour, minute), tzinfo=now.tzinfo)

    match = DATE_TIME_RE.fullmatch(spec)
    if match:
        day = dt.date.fromisoformat(match.group("date"))
        hour, minute = _parse_clock(match.group("time"))
        candidate = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=now.tzinfo)
        if candidate <= now:
            raise ValueError("date/time is in the past")
        return candidate

    match = WEEKDAY_TIME_RE.fullmatch(spec)
    if match:
        weekday_name = match.group("weekday").lower()
        if weekday_name not in WEEKDAYS:
            raise ValueError(f"unknown weekday: {match.group('weekday')}")
        hour, minute = _parse_clock(match.group("time"))
        wanted = WEEKDAYS[weekday_name]
        days_ahead = (wanted - now.weekday()) % 7
        candidate_date = now.date() + dt.timedelta(days=days_ahead)
        candidate = dt.datetime.combine(candidate_date, dt.time(hour, minute), tzinfo=now.tzinfo)
        if candidate <= now:
            candidate += dt.timedelta(days=7)
        return candidate

    raise ValueError("use 15h, 1h30m, HH:MM, fri 18:00, tomorrow 09:00, or YYYY-MM-DD HH:MM")


def state_dir() -> Path:
    value = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if value:
        path = Path(value).expanduser()
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
        path = base / "herdr" / "plugins" / "codex.scheduler"
    path.mkdir(parents=True, exist_ok=True)
    return path


def jobs_path() -> Path:
    return state_dir() / "jobs.json"


def lock_path() -> Path:
    return state_dir() / "jobs.lock"


def number_counter_path() -> Path:
    return state_dir() / "next-job-number"


def _history_stamp(job: dict[str, Any]) -> str:
    return str(
        job.get("sent_at")
        or job.get("cancelled_at")
        or job.get("updated_at")
        or job.get("created_at")
        or ""
    )


def prune_history(jobs: list[dict[str, Any]], limit: int = HISTORY_STORAGE_LIMIT) -> None:
    """Keep every pending job and only the newest bounded finished history."""
    finished = [job for job in jobs if job.get("status") != "pending"]
    if len(finished) <= limit:
        return
    keep_ids = {
        id(job)
        for job in sorted(finished, key=_history_stamp, reverse=True)[:limit]
    }
    jobs[:] = [job for job in jobs if job.get("status") == "pending" or id(job) in keep_ids]


def _read_next_number(jobs: list[dict[str, Any]]) -> int:
    minimum = max(
        (value for value in (job.get("number") for job in jobs) if isinstance(value, int) and value > 0),
        default=0,
    ) + 1
    try:
        stored = int(number_counter_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        stored = minimum
    return max(minimum, stored, 1)


def _write_next_number(value: int) -> None:
    path = number_counter_path()
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def allocate_job_number(jobs: list[dict[str, Any]]) -> int:
    value = _read_next_number(jobs)
    _write_next_number(value + 1)
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


@contextlib.contextmanager
def locked_jobs() -> Iterator[list[dict[str, Any]]]:
    lock_file = lock_path().open("a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    try:
        path = jobs_path()
        if path.exists():
            try:
                jobs = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(jobs, list):
                    jobs = []
            except (json.JSONDecodeError, OSError):
                jobs = []
        else:
            jobs = []
        for job in jobs:
            if isinstance(job, dict):
                normalize_job(job)
        assign_job_numbers(jobs)
        prune_history(jobs)
        # Migration may have assigned numbers before the monotonic counter existed.
        _write_next_number(_read_next_number(jobs))
        yield jobs
        prune_history(jobs)
        atomic_write_json(path, jobs)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def run_herdr(herdr: str, socket_path: str | None, args: list[str],
              timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if socket_path:
        env["HERDR_SOCKET_PATH"] = socket_path
    return subprocess.run(
        [herdr, *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def parse_herdr_json(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(detail)
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Herdr returned invalid JSON: {proc.stdout.strip()[:200]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Herdr returned an unexpected response")
    return value


def extract_agent(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if isinstance(result, dict):
        agent = result.get("agent")
        if isinstance(agent, dict):
            return agent
    raise RuntimeError("Herdr did not return a live agent")


def extract_pane(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if isinstance(result, dict):
        pane = result.get("pane")
        if isinstance(pane, dict):
            return pane
    raise RuntimeError("Herdr did not return pane information")


def enrich_agent_from_pane(agent: dict[str, Any], pane: dict[str, Any] | None) -> dict[str, Any]:
    """Merge pane-owned identity/presentation fields into an AgentInfo snapshot.

    Herdr exposes native agent_session references from both agent.get and pane.get.
    In practice a pane-level session report can arrive before (or independently of) the
    agent view, so use the pane snapshot as an authoritative fallback instead of
    guessing the agent session from terminal text or local agent files.
    """
    merged = dict(agent)
    if not isinstance(pane, dict):
        return merged

    for key in ("agent_session", "foreground_cwd", "terminal_title", "terminal_title_stripped"):
        if merged.get(key) in (None, "") and pane.get(key) not in (None, ""):
            merged[key] = pane.get(key)

    if not _first_string(merged, ("state", "status", "agent_status")):
        state = _first_string(pane, ("agent_status", "state", "status"))
        if state:
            merged["state"] = state
    return merged


def live_agent_snapshot(
    herdr: str, socket_path: str | None, pane_id: str, timeout: float = 10.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_herdr,
) -> dict[str, Any]:
    """Return a live agent snapshot with pane-owned native identity.

    Agent detection and native session reporting are independent in Herdr. A pane may
    therefore carry a valid official ``agent_session`` even when ``agent get`` is
    temporarily unavailable. Query both surfaces and keep the pane identity usable.
    """
    agent: dict[str, Any] | None = None
    agent_error: Exception | None = None
    try:
        response = runner(herdr, socket_path, ["agent", "get", pane_id], timeout)
        agent = extract_agent(parse_herdr_json(response))
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        agent_error = exc

    pane: dict[str, Any] | None = None
    pane_error: Exception | None = None
    try:
        response = runner(herdr, socket_path, ["pane", "get", pane_id], timeout)
        pane = extract_pane(parse_herdr_json(response))
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        pane_error = exc

    if agent is not None:
        return enrich_agent_from_pane(agent, pane)

    pane_snapshot = enrich_agent_from_pane({}, pane)
    if agent_session(pane_snapshot) is not None:
        return pane_snapshot

    if agent_error is not None:
        raise RuntimeError(str(agent_error)) from agent_error
    if pane_error is not None:
        raise RuntimeError(str(pane_error)) from pane_error
    raise RuntimeError("Herdr did not return a live agent or native session identity")


def agent_session(agent: dict[str, Any]) -> dict[str, Any] | None:
    session = agent.get("agent_session")
    return session if isinstance(session, dict) else None


def agent_kind(agent: dict[str, Any]) -> str | None:
    # Live detection takes precedence over a session reference left by an old
    # occupant of this pane. Display names and terminal titles are not identity.
    value = _first_string(agent, ("agent", "kind", "agent_kind"))
    if value:
        return value.lower()
    session = agent_session(agent)
    value = _first_string(session, ("agent",)) if session else None
    if not value:
        value = _first_string(agent, ("label",))
    return value.lower() if value else None


def is_supported_agent(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) in AGENT_NAMES


def is_codex(agent: dict[str, Any]) -> bool:
    return agent_kind(agent) == "codex"


def agent_name(kind: str | None) -> str:
    return AGENT_NAMES.get(kind, kind or "Agent")


def job_agent_kind(job: dict[str, Any]) -> str:
    target = job.get("target") if isinstance(job.get("target"), dict) else {}
    value = _first_string(target, ("agent",))
    if value:
        return value.lower()
    session = job.get("agent_session")
    value = _first_string(session, ("agent",)) if isinstance(session, dict) else None
    # Records written by the Codex-only scheduler may not have an agent field.
    return value.lower() if value else "codex"


def same_session(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    expected_value = expected.get("value")
    actual_value = actual.get("value")
    if expected_value is not None and actual_value is not None:
        return expected_value == actual_value and expected.get("agent") == actual.get("agent")
    return expected == actual


def _first_string(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def target_snapshot(pane: str, agent: dict[str, Any]) -> dict[str, Any]:
    session = agent_session(agent)
    session_value = None
    if session:
        value = session.get("value")
        if value is not None:
            session_value = str(value)
    return {
        "pane": pane,
        "agent": agent_kind(agent),
        "name": _first_string(agent, ("name", "display_name", "label")),
        "state": _first_string(agent, ("state", "status", "agent_status")),
        "session": session,
        "session_id": session_value,
        "cwd": _first_string(agent, ("foreground_cwd", "cwd")),
        "title": _first_string(agent, ("terminal_title_stripped", "terminal_title", "title")),
    }


def agent_state(agent: dict[str, Any]) -> str | None:
    value = _first_string(agent, ("state", "status", "agent_status"))
    return value.lower() if value else None


def show_notification(herdr: str, socket_path: str | None, title: str, body: str = "",
                      sound: str = "none") -> bool:
    args = ["notification", "show", title]
    if body:
        args += ["--body", body]
    if sound in ("none", "done", "request"):
        args += ["--sound", sound]
    try:
        proc = run_herdr(herdr, socket_path, args, timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def metadata_panes_path() -> Path:
    return state_dir() / "metadata-panes.json"


def _remaining_text(due_at: Any, now: dt.datetime) -> str:
    try:
        due = dt.datetime.fromisoformat(str(due_at))
    except (TypeError, ValueError):
        return "soon"
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    seconds = max(0, int((due - now).total_seconds()))
    minutes = max(1, seconds // 60) if seconds else 0
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if seconds <= 0:
        return "due"
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def sync_schedule_metadata(jobs: list[dict[str, Any]], herdr: str, socket_path: str | None) -> None:
    """Report a non-invasive scheduler token for panes with pending jobs.

    Herdr does not expose a dedicated persistent plugin badge. Metadata tokens are
    display-only and become visible only when the user's sidebar layout includes
    `$scheduler`; we never rewrite pane names, titles, or semantic agent state.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        if job.get("status") != "pending":
            continue
        pane = job.get("pane")
        if isinstance(pane, str) and pane:
            groups.setdefault(pane, []).append(job)

    path = metadata_panes_path()
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        remembered = (
            {str(value) for value in previous if isinstance(value, str)}
            if isinstance(previous, list) else set()
        )
    except (OSError, json.JSONDecodeError):
        remembered = set()

    now = local_now()
    for pane in sorted(set(groups) | remembered):
        pending = groups.get(pane, [])
        if pending:
            next_job = min(pending, key=lambda job: str(job.get("due_at") or ""))
            count = len(pending)
            noun = "scheduled"
            value = f"⏱ {count} {noun} · next in {_remaining_text(next_job.get('due_at'), now)}"
            args = [
                "pane", "report-metadata", pane,
                "--source", "plugin:codex.scheduler",
                "--token", f"scheduler={value}",
            ]
        else:
            args = [
                "pane", "report-metadata", pane,
                "--source", "plugin:codex.scheduler",
                "--clear-token", "scheduler",
            ]
        try:
            run_herdr(herdr, socket_path, args, timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    atomic_write_json(path, sorted(groups))


def assign_job_numbers(jobs: list[dict[str, Any]]) -> None:
    """Give every job a stable, human-facing positive integer number."""
    used: set[int] = set()
    missing: list[dict[str, Any]] = []

    for job in jobs:
        value = job.get("number")
        if isinstance(value, int) and value > 0 and value not in used:
            used.add(value)
        else:
            job["number"] = None
            missing.append(job)

    next_number = 1
    for job in missing:
        while next_number in used:
            next_number += 1
        job["number"] = next_number
        used.add(next_number)
        next_number += 1


def next_job_number(jobs: list[dict[str, Any]]) -> int:
    """Compatibility helper; new jobs use the persistent monotonic allocator."""
    return _read_next_number(jobs)


def normalize_job(job: dict[str, Any]) -> None:
    # Migrate 0.1.x records in place while preserving delivery compatibility.
    if "schedule_spec" not in job:
        job["schedule_spec"] = None
    if "updated_at" not in job:
        job["updated_at"] = job.get("created_at")
    if "delivery_state" not in job:
        job["delivery_state"] = None
    if "delivery_note" not in job:
        job["delivery_note"] = None
    if not isinstance(job.get("target"), dict):
        session = job.get("agent_session") if isinstance(job.get("agent_session"), dict) else None
        job["target"] = {
            "pane": job.get("pane"),
            "agent": job_agent_kind(job),
            "name": None,
            "state": None,
            "session": session,
            "session_id": str(session.get("value")) if session and session.get("value") is not None else None,
            "cwd": None,
            "title": None,
        }
    else:
        job["target"]["agent"] = job_agent_kind(job)


def create_job(
    when: dt.datetime,
    prompt: str,
    pane: str,
    herdr: str,
    socket_path: str | None,
    agent: dict[str, Any],
    schedule_spec: str | None = None,
) -> dict[str, Any]:
    if not is_supported_agent(agent):
        raise ValueError("schedule target must be a live Codex or Claude Code agent")
    now = local_now()
    target = target_snapshot(pane, agent)
    job = {
        "id": uuid.uuid4().hex[:8],
        "number": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "due_at": when.isoformat(),
        "schedule_spec": schedule_spec,
        "prompt": prompt,
        "pane": pane,
        "herdr": herdr,
        "socket_path": socket_path,
        "agent_session": agent_session(agent),
        "target": target,
        "status": "pending",
        "attempts": 0,
        "last_attempt_at": None,
        "last_error": None,
        "sent_at": None,
        "cancelled_at": None,
        "delivery_state": None,
        "delivery_note": None,
    }
    with locked_jobs() as jobs:
        job["number"] = allocate_job_number(jobs)
        jobs.append(job)
    return json.loads(json.dumps(job))


def read_jobs() -> list[dict[str, Any]]:
    with locked_jobs() as jobs:
        return json.loads(json.dumps(jobs))


def mutate_job(job_id: str, mutator: Callable[[dict[str, Any]], None]) -> bool:
    with locked_jobs() as jobs:
        for job in jobs:
            if job.get("id") == job_id:
                mutator(job)
                return True
    return False


def update_job(job_id: str, when: dt.datetime | None = None, prompt: str | None = None,
               schedule_spec: str | None = None) -> bool:
    now = local_now().isoformat()

    def update(job: dict[str, Any]) -> None:
        if job.get("status") != "pending":
            return
        if when is not None:
            job["due_at"] = when.isoformat()
            job["schedule_spec"] = schedule_spec
        if prompt is not None:
            job["prompt"] = prompt
        job["updated_at"] = now
        job["last_attempt_at"] = None
        job["last_error"] = None
        job["delivery_state"] = None
        job["delivery_note"] = None

    with locked_jobs() as jobs:
        for job in jobs:
            if job.get("id") == job_id and job.get("status") == "pending":
                update(job)
                return True
    return False


def cancel_job(job_id: str) -> bool:
    now = local_now().isoformat()
    with locked_jobs() as jobs:
        for job in jobs:
            if job.get("id") == job_id and job.get("status") == "pending":
                job["status"] = "cancelled"
                job["cancelled_at"] = now
                job["updated_at"] = now
                job["delivery_state"] = None
                job["delivery_note"] = None
                return True
    return False


def delete_job(job_id: str) -> bool:
    with locked_jobs() as jobs:
        for index, job in enumerate(jobs):
            if job.get("id") == job_id:
                jobs.pop(index)
                return True
    return False


def get_job(job_id: str) -> dict[str, Any] | None:
    for job in read_jobs():
        if job.get("id") == job_id:
            return job
    return None


def job_matches_target(job: dict[str, Any], pane: str | None, session: dict[str, Any] | None) -> bool:
    if not pane or job.get("pane") != pane:
        return False
    expected = job.get("agent_session") if isinstance(job.get("agent_session"), dict) else None
    if expected is not None:
        return session is not None and same_session(expected, session)
    return True
