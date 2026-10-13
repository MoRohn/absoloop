"""Cross-process run control: live PID tracking and cancel requests.

A long-running harness writes `.absoloop/runs/<run-id>/live.json` with the
orchestrator PID and every active provider child (pid + process-group id).
`absoloop cancel <run-id>` from another terminal:

1. writes `cancel.requested` so in-process watchdogs mark the run cancelled
2. sends SIGTERM/SIGKILL to each recorded process group
3. updates live.json + the run manifest so the record stays valid

No credentials or environment dumps ever land in these files.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import time
from typing import Any, Dict, List, Optional


LIVE_NAME = "live.json"
CANCEL_NAME = "cancel.requested"


def live_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / LIVE_NAME


def cancel_flag_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / CANCEL_NAME


def pid_alive(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True


def read_live(run_dir: pathlib.Path) -> Dict[str, Any]:
    path = live_path(run_dir)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


_live_lock = None  # process-local lock; cross-process uses unique temp names


def _get_live_lock():
    global _live_lock
    if _live_lock is None:
        import threading
        _live_lock = threading.Lock()
    return _live_lock


def _write_live_unlocked(run_dir: pathlib.Path, payload: Dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = live_path(run_dir)
    data = dict(payload)
    data["heartbeat"] = time.time()
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = run_dir / f".live.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_live(run_dir: pathlib.Path, payload: Dict[str, Any]) -> None:
    """Atomically replace live.json. Unique temp names + a process lock keep
    parallel race/council lanes from clobbering each other's child lists."""
    with _get_live_lock():
        _write_live_unlocked(run_dir, payload)


def begin_run(run_dir: pathlib.Path, *, run_id: str, strategy: str,
              providers: List[str]) -> Dict[str, Any]:
    """Seed live.json when a harness workflow starts."""
    payload = {
        "run_id": run_id,
        "strategy": strategy,
        "status": "running",
        "orchestrator_pid": os.getpid(),
        "providers": list(providers),
        "children": [],
        "started_at": time.time(),
        "cancel_requested": False,
    }
    write_live(run_dir, payload)
    # Drop a stale cancel flag from a previous attempt at this run id.
    flag = cancel_flag_path(run_dir)
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass
    return payload


def register_child(run_dir: pathlib.Path, *, role: str, provider: str,
                   pid: int, pgid: Optional[int] = None) -> None:
    with _get_live_lock():
        live = read_live(run_dir) or {
            "run_id": run_dir.name, "status": "running",
            "orchestrator_pid": os.getpid(), "children": []}
        children = [c for c in live.get("children", [])
                    if isinstance(c, dict) and c.get("role") != role]
        children.append({
            "role": role,
            "provider": provider,
            "pid": pid,
            "pgid": pgid if pgid is not None else pid,
            "started_at": time.time(),
        })
        live["children"] = children
        live["status"] = "running"
        _write_live_unlocked(run_dir, live)


def unregister_child(run_dir: pathlib.Path, role: str) -> None:
    with _get_live_lock():
        live = read_live(run_dir)
        if not live:
            return
        live["children"] = [c for c in live.get("children", [])
                            if isinstance(c, dict) and c.get("role") != role]
        _write_live_unlocked(run_dir, live)


def finish_run(run_dir: pathlib.Path, status: str) -> None:
    with _get_live_lock():
        live = read_live(run_dir)
        if not live:
            live = {"run_id": run_dir.name, "children": []}
        live["status"] = status
        live["finished_at"] = time.time()
        live["children"] = []
        _write_live_unlocked(run_dir, live)


def request_cancel(run_dir: pathlib.Path) -> None:
    """Ask the in-process watchdog to mark the run cancelled (best-effort)."""
    cancel_flag_path(run_dir).write_text(
        json.dumps({"requested_at": time.time(), "by_pid": os.getpid()}) + "\n",
        encoding="utf-8")
    with _get_live_lock():
        live = read_live(run_dir)
        if live:
            live["cancel_requested"] = True
            live["status"] = "cancelling"
            _write_live_unlocked(run_dir, live)


def cancel_requested(run_dir: pathlib.Path) -> bool:
    if cancel_flag_path(run_dir).is_file():
        return True
    return bool(read_live(run_dir).get("cancel_requested"))


def kill_process_group(pid: int, pgid: Optional[int] = None) -> bool:
    """Terminate a process group (Unix) or process tree (Windows).

    Returns True when a signal/kill was delivered or the process was already
    gone.
    """
    target = pgid if isinstance(pgid, int) and pgid > 0 else pid
    if target <= 0:
        return False
    try:
        if os.name != "nt":
            try:
                os.killpg(target, signal.SIGTERM)
            except ProcessLookupError:
                return True
            for _ in range(20):
                if not pid_alive(pid):
                    return True
                time.sleep(0.05)
            try:
                os.killpg(target, signal.SIGKILL)
            except ProcessLookupError:
                return True
            return True
        from .platform_util import kill_process_tree
        # Windows has no session to signal; kill the recorded pid's tree.
        return kill_process_tree(pid) or not pid_alive(pid)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            if os.name == "nt":
                from .platform_util import kill_process_tree
                return kill_process_tree(pid) or not pid_alive(pid)
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return not pid_alive(pid)


def cancel_run(run_dir: pathlib.Path) -> Dict[str, Any]:
    """Cross-process cancel: flag + kill every recorded child and the
    orchestrator if it is still alive. Returns a summary for the CLI."""
    live = read_live(run_dir)
    if not live:
        return {"ok": False, "reason": "no_live_state",
                "message": "run has no live.json — it may have finished already"}
    status = str(live.get("status", ""))
    if status in ("completed", "failed", "cancelled", "timeout", "finished"):
        return {"ok": False, "reason": "already_finished",
                "message": f"run already finished with status {status!r}",
                "status": status}

    request_cancel(run_dir)
    killed: List[Dict[str, Any]] = []
    for child in live.get("children", []):
        if not isinstance(child, dict):
            continue
        pid = child.get("pid")
        pgid = child.get("pgid")
        if not isinstance(pid, int):
            continue
        was_alive = pid_alive(pid)
        delivered = kill_process_group(pid, pgid if isinstance(pgid, int) else pid)
        killed.append({
            "role": child.get("role"),
            "provider": child.get("provider"),
            "pid": pid,
            "was_alive": was_alive,
            "signal_delivered": delivered,
        })

    orch_pid = live.get("orchestrator_pid")
    orch_killed = False
    if isinstance(orch_pid, int) and orch_pid != os.getpid() and pid_alive(orch_pid):
        # Soft-stop the orchestrator after children: it should observe the
        # cancelled children and finalize; if it is stuck, SIGTERM it.
        # On Windows that TerminateProcess can still signal this console.
        try:
            if os.name == "nt":
                from .platform_util import child_teardown
                with child_teardown():
                    os.kill(orch_pid, signal.SIGTERM)
            else:
                os.kill(orch_pid, signal.SIGTERM)
            orch_killed = True
        except OSError:
            pass

    live = read_live(run_dir)
    live["status"] = "cancelled"
    live["cancelled_at"] = time.time()
    live["children"] = []
    write_live(run_dir, live)
    if os.name == "nt":
        # A child that shared the console may already have tripped the
        # sigint event. Clear it before the caller blocks in Thread.join.
        from .platform_util import clear_spurious_keyboard_interrupt
        clear_spurious_keyboard_interrupt()
    return {
        "ok": True,
        "reason": "cancelled",
        "killed": killed,
        "orchestrator_pid": orch_pid,
        "orchestrator_signaled": orch_killed,
    }


def is_run_live(run_dir: pathlib.Path, *, stale_after: float = 90.0) -> bool:
    """True when live.json says running and either a child or the orchestrator
    is still alive (or the heartbeat is fresh enough that we cannot tell)."""
    live = read_live(run_dir)
    if not live or live.get("status") not in ("running", "cancelling"):
        return False
    orch = live.get("orchestrator_pid")
    if pid_alive(orch if isinstance(orch, int) else None):
        return True
    for child in live.get("children", []):
        if isinstance(child, dict) and pid_alive(child.get("pid")):
            return True
    heartbeat = live.get("heartbeat")
    if isinstance(heartbeat, (int, float)) and time.time() - heartbeat < stale_after:
        return True
    return False
