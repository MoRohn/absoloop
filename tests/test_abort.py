"""Mission abort: stop a live loop and mark state STOPPED."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from tests._load import load_cli

cli = load_cli()


def _write(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


class AbortCommandTests(unittest.TestCase):
    def test_abort_noop_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp)
            _write(project / ".absoloop" / "state.json", {
                "status": "READY",
                "iteration": 0,
                "mission_id": "ABS-1",
            })
            code = cli.abort_command(["-C", str(project), "--yes"])
            self.assertEqual(code, 0)
            state = json.loads(
                (project / ".absoloop" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "READY")

    def test_abort_signals_live_runner_and_marks_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp)
            now = time.time()
            fake_pid = 424242
            _write(project / ".absoloop" / "state.json", {
                "status": "EXECUTING",
                "iteration": 2,
                "mission_id": "ABS-7",
                "started_at": now - 60,
                "cost_usd": 1.5,
            })
            _write(project / ".absoloop" / "tmp" / "monitor.json", {
                "schema": 1,
                "pid": fake_pid,
                "heartbeat_ts": now,
                "status": "EXECUTING",
                "phase": "building",
                "mission_id": "ABS-7",
            })
            with mock.patch.object(cli, "pid_alive", side_effect=[True, True, False]), \
                 mock.patch.object(cli, "terminate_mission_pid",
                                   return_value=True) as term:
                code = cli.abort_command(["-C", str(project), "--yes"])
            self.assertEqual(code, 0)
            term.assert_called_once_with(fake_pid)
            state = json.loads(
                (project / ".absoloop" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "STOPPED")
            self.assertEqual(state["stop_reason"], "human_aborted")
            monitor = json.loads(
                (project / ".absoloop" / "tmp" / "monitor.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(monitor["status"], "STOPPED")
            self.assertEqual(monitor["stop_reason"], "human_aborted")
            self.assertEqual(monitor["phase"], "stopped")
            ledger = (project / ".absoloop" / "ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("human_aborted", ledger)

    def test_abort_clears_stuck_executing_without_live_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = pathlib.Path(tmp)
            _write(project / ".absoloop" / "state.json", {
                "status": "EXECUTING",
                "iteration": 1,
                "mission_id": "ABS-9",
                "started_at": time.time() - 10_000,
            })
            _write(project / ".absoloop" / "tmp" / "monitor.json", {
                "pid": 1,
                "heartbeat_ts": time.time() - 10_000,
                "status": "EXECUTING",
            })
            with mock.patch.object(cli, "terminate_mission_pid") as term, \
                 mock.patch.object(cli, "pid_alive", return_value=True):
                code = cli.abort_command(["-C", str(project), "--yes"])
            self.assertEqual(code, 0)
            # Stale heartbeat → do not signal a possibly-reused PID.
            term.assert_not_called()
            state = json.loads(
                (project / ".absoloop" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "STOPPED")

    @unittest.skipIf(
        os.name == "nt",
        "POSIX-only: os.getpgid and os.killpg do not exist on Windows. "
        "terminate_mission_pid uses kill_process_tree there and never "
        "signals a process group, so this shell-group guard is not reachable.",
    )
    def test_terminate_skips_killpg_when_not_group_leader(self):
        """Never SIGTERM the operator's shell process group."""
        with mock.patch.object(cli, "pid_alive", side_effect=[True, False]), \
             mock.patch("os.getpgid", return_value=1), \
             mock.patch("os.killpg") as killpg, \
             mock.patch("os.kill") as kill, \
             mock.patch.object(cli, "_child_pids", return_value=[99]):
            ok = cli.terminate_mission_pid(50, timeout=0.2)
        self.assertTrue(ok)
        killpg.assert_not_called()
        kill.assert_any_call(99, mock.ANY)
        kill.assert_any_call(50, mock.ANY)

    def test_next_step_hint_for_stopped(self):
        hint = cli.next_step_hint({"status": "STOPPED"})
        self.assertIn("resume", hint.lower())
        self.assertIn("abort", hint.lower())


if __name__ == "__main__":
    unittest.main()
