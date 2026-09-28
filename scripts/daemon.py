from __future__ import annotations

import contextlib
import fcntl
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scheduler_core import (  # noqa: E402
    agent_kind,
    agent_name,
    agent_state,
    job_agent_kind,
    live_agent_snapshot,
    local_now,
    locked_jobs,
    run_herdr,
    same_session,
    show_notification,
    state_dir,
    sync_schedule_metadata,
    target_snapshot,
)

POLL_SECONDS = 5
RETRY_SECONDS = 30
MAX_RETRY_HOURS = 24


def pid_path() -> Path:
    return state_dir() / "daemon.pid"


def daemon_lock_path() -> Path:
    return state_dir() / "daemon.lock"


def log_path() -> Path:
    return state_dir() / "daemon.log"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def existing_pid() -> int | None:
    path = pid_path()
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def start() -> int:
    state_dir().mkdir(parents=True, exist_ok=True)
    with daemon_lock_path().open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if existing_pid() is not None:
            return 0

        log = log_path().open("a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "run"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log.close()
        pid_path().write_text(str(proc.pid), encoding="utf-8")
    return 0


def stop() -> int:
    pid = existing_pid()
    if pid is None:
        with contextlib.suppress(FileNotFoundError):
            pid_path().unlink()
        return 0
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    with contextlib.suppress(FileNotFoundError):
        pid_path().unlink()
    return 0


def append_log(message: str) -> None:
    stamp = local_now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def mark_retry(job: dict, error: str, now: dt.datetime) -> None:
    job["attempts"] = int(job.get("attempts") or 0) + 1
    job["last_attempt_at"] = now.isoformat()
    job["last_error"] = error[:1000]


def set_waiting(job: dict, state: str, now: dt.datetime) -> None:
    name = agent_name(job_agent_kind(job))
    note = f"{name} is {state}; waiting until it is ready before delivery"
    if job.get("delivery_note") == note:
        return
    job["delivery_state"] = "waiting_for_ready"
    job["delivery_note"] = note
    job["updated_at"] = now.isoformat()
    job["last_error"] = None


def clear_waiting(job: dict) -> None:
    job["delivery_state"] = None
    job["delivery_note"] = None


def notify_job(job: dict, title: str, body: str, sound: str = "none") -> None:
    herdr = job.get("herdr") or "herdr"
    socket_path = job.get("socket_path")
    show_notification(herdr, socket_path, title, body, sound=sound)


def process_due_job(job: dict, now: dt.datetime) -> None:
    expected_kind = job_agent_kind(job)
    name = agent_name(expected_kind)
    due = dt.datetime.fromisoformat(job["due_at"])
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)

    last_attempt = job.get("last_attempt_at")
    if last_attempt:
        try:
            last = dt.datetime.fromisoformat(last_attempt)
            if (now - last).total_seconds() < RETRY_SECONDS:
                return
        except ValueError:
            pass

    if now - due > dt.timedelta(hours=MAX_RETRY_HOURS):
        job["status"] = "failed"
        job["updated_at"] = now.isoformat()
        job["last_attempt_at"] = now.isoformat()
        job["last_error"] = "delivery window expired after 24 hours"
        clear_waiting(job)
        append_log(f"job {job.get('number', '?')} failed: delivery window expired")
        notify_job(job, f"{name} schedule failed", f"#{job.get('number', '?')} · delivery window expired", "request")
        return

    herdr = job.get("herdr") or "herdr"
    socket_path = job.get("socket_path")
    pane = job["pane"]

    try:
        current = live_agent_snapshot(herdr, socket_path, pane, runner=run_herdr)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        mark_retry(job, f"agent unavailable: {exc}", now)
        return

    if agent_kind(current) != expected_kind:
        job["status"] = "stale"
        job["updated_at"] = now.isoformat()
        job["last_attempt_at"] = now.isoformat()
        job["last_error"] = f"pane is no longer running {name}"
        clear_waiting(job)
        append_log(f"job {job.get('number', '?')} stale: pane no longer runs {name}")
        notify_job(job, f"{name} schedule stale", f"#{job.get('number', '?')} · pane no longer runs {name}", "request")
        return

    expected_session = job.get("agent_session") if isinstance(job.get("agent_session"), dict) else None
    actual_session = current.get("agent_session") if isinstance(current.get("agent_session"), dict) else None
    if not same_session(expected_session, actual_session):
        job["status"] = "stale"
        job["updated_at"] = now.isoformat()
        job["last_attempt_at"] = now.isoformat()
        job["last_error"] = f"{name} session identity changed"
        clear_waiting(job)
        append_log(f"job {job.get('number', '?')} stale: session identity changed")
        notify_job(job, f"{name} schedule stale", f"#{job.get('number', '?')} · {name} session changed", "request")
        return

    # Some agent/Herdr combinations do not expose a native session id. In that
    # case, verify the project captured when the job was created before sending.
    if expected_session is None:
        expected_target = job.get("target") if isinstance(job.get("target"), dict) else {}
        expected_cwd = expected_target.get("cwd")
        if expected_cwd:
            current_cwd = target_snapshot(pane, current).get("cwd")
            if not current_cwd:
                mark_retry(job, f"cannot verify {name} target project cwd", now)
                return
            if os.path.realpath(str(current_cwd)) != os.path.realpath(str(expected_cwd)):
                job["status"] = "stale"
                job["updated_at"] = now.isoformat()
                job["last_attempt_at"] = now.isoformat()
                job["last_error"] = f"{name} target project changed"
                clear_waiting(job)
                append_log(f"job {job.get('number', '?')} stale: project cwd changed")
                notify_job(job, f"{name} schedule stale", f"#{job.get('number', '?')} · project changed", "request")
                return

    # Herdr's `agent prompt` can submit input while an agent is working. The
    # scheduler intentionally does not use that behavior: defer until Herdr
    # reports a ready state so a scheduled prompt cannot intentionally preempt
    # the active turn. A real-Herdr smoke test is still required for the narrow
    # state-change race between this check and the prompt call.
    state = agent_state(current)
    if state in ("working", "blocked"):
        set_waiting(job, state, now)
        return
    if state not in ("idle", "done"):
        mark_retry(job, f"cannot verify {name} is ready (state={state or 'unknown'})", now)
        return

    clear_waiting(job)
    proc = run_herdr(herdr, socket_path, ["agent", "prompt", pane, job["prompt"]], timeout=15.0)
    if proc.returncode == 0:
        job["status"] = "sent"
        job["updated_at"] = now.isoformat()
        job["sent_at"] = now.isoformat()
        job["last_attempt_at"] = now.isoformat()
        job["last_error"] = None
        job["attempts"] = int(job.get("attempts") or 0) + 1
        append_log(f"job {job.get('number', '?')} sent to {pane}")
        body = f"#{job.get('number', '?')} · {job.get('prompt') or 'Continue'}"
        notify_job(job, f"{name} prompt delivered", body, "done")
        return

    detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
    mark_retry(job, detail, now)


def run() -> int:
    pid_path().write_text(str(os.getpid()), encoding="utf-8")

    def handle_term(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)
    append_log("daemon started")
    last_metadata_sync = 0.0

    try:
        while True:
            now = local_now()
            metadata_dirty = False
            snapshot: list[dict] = []
            with locked_jobs() as jobs:
                for job in jobs:
                    if job.get("status") != "pending":
                        continue
                    try:
                        due = dt.datetime.fromisoformat(job["due_at"])
                    except (KeyError, ValueError, TypeError):
                        job["status"] = "failed"
                        job["updated_at"] = now.isoformat()
                        job["last_error"] = "invalid due_at"
                        metadata_dirty = True
                        continue
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=now.tzinfo)
                    if due <= now:
                        before = job.get("status")
                        process_due_job(job, now)
                        if job.get("status") != before:
                            metadata_dirty = True
                snapshot = json.loads(json.dumps(jobs))

            monotonic = time.monotonic()
            if metadata_dirty or monotonic - last_metadata_sync >= 60.0:
                herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
                socket_path = os.environ.get("HERDR_SOCKET_PATH")
                try:
                    sync_schedule_metadata(snapshot, herdr, socket_path)
                except Exception as exc:
                    append_log(f"metadata sync failed: {exc}")
                last_metadata_sync = monotonic
            time.sleep(POLL_SECONDS)
    finally:
        with contextlib.suppress(FileNotFoundError):
            if pid_path().read_text().strip() == str(os.getpid()):
                pid_path().unlink()
        append_log("daemon stopped")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "start":
        return start()
    if command == "stop":
        return stop()
    if command == "run":
        return run()
    print("usage: daemon.py [start|stop|run]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
