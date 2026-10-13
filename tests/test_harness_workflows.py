"""Multi-provider workflow tests (single / review / race / council) driven
entirely by the fake provider — no credentials, no network. Also includes
the opt-in live smoke test, gated by environment variables."""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Harness finishes fire a desktop chime; keep unit tests silent.
os.environ.setdefault("ABSOLOOP_CHIME", "0")

from absoloop_harness.config import Config, DEFAULTS
from absoloop_harness.orchestrator import Candidate, GateResult, Orchestrator
from absoloop_harness.workspace import RunStore

FAKE = pathlib.Path(__file__).resolve().parent / "fakes" / "fake_provider.py"


def fake_config(gate_command: str) -> Config:
    FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    import copy
    values = copy.deepcopy(DEFAULTS)
    for provider in ("grok", "claude", "codex"):
        values["providers"][provider] = {
            "command": str(FAKE), "model": "", "timeout_seconds": 60,
            "env_allowlist": ["FAKE_PROVIDER_MODE", "FAKE_PROVIDER_SESSION",
                              "FAKE_PROVIDER_EDIT_FILE"],
        }
    values["gates"]["required"] = ["tests"]
    values["gates"]["commands"] = {"tests": gate_command}
    values["artifacts"]["retention_runs"] = 50
    return Config(values, {})


def make_repo(root: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)


class SingleWorkflow(unittest.TestCase):
    def test_single_produces_artifacts_and_patch(self):
        os.environ["FAKE_PROVIDER_MODE"] = "edit"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            orch = Orchestrator(root, fake_config("test -f fake_edit.txt"))
            manifest = orch.single("grok", "make an edit", "edit")
            self.assertEqual(manifest["selected_candidate"], "grok")
            self.assertEqual(manifest["candidates"][0]["status"], "completed")
            self.assertEqual(manifest["candidates"][0]["session_id"],
                             "fake-session-123")
            self.assertTrue(manifest["candidates"][0]["gates"][0]["passed"])
            run_dir = root / ".absoloop" / "runs" / manifest["run_id"]
            for artifact in ("manifest.json", "events.jsonl", "summary.md",
                             "candidates/grok/final.json",
                             "candidates/grok/diff.patch",
                             "candidates/grok/test.log",
                             "candidates/grok/stderr.log"):
                self.assertTrue((run_dir / artifact).is_file(), artifact)
            # worktrees cleaned by default
            self.assertFalse((root / ".absoloop" / "worktrees").exists())
            # repo root untouched until `absoloop apply`
            self.assertFalse((root / "fake_edit.txt").exists())

    def test_failing_gate_rejects_candidate(self):
        os.environ["FAKE_PROVIDER_MODE"] = "edit"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            orch = Orchestrator(root, fake_config("false"))
            manifest = orch.single("claude", "do a thing", "edit")
            self.assertIsNone(manifest["selected_candidate"])
            self.assertFalse(manifest["candidates"][0]["gates"][0]["passed"])


class RaceWorkflow(unittest.TestCase):
    def test_race_isolates_and_selects_deterministically(self):
        os.environ["FAKE_PROVIDER_MODE"] = "edit"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            orch = Orchestrator(root, fake_config("test -f fake_edit.txt"))
            manifest = orch.race(["grok", "claude", "codex"], "race it", "edit")
            roles = sorted(c["role"] for c in manifest["candidates"])
            self.assertEqual(roles, ["claude", "codex", "grok"])
            run_dir = root / ".absoloop" / "runs" / manifest["run_id"]
            for candidate in manifest["candidates"]:
                detail = candidate["role"]
                if candidate["status"] != "completed":
                    err_path = (run_dir / "candidates" / candidate["role"]
                                / "stderr.log")
                    if err_path.is_file():
                        detail = f"{detail}: {err_path.read_text(encoding='utf-8')[-800:]}"
                self.assertEqual(candidate["status"], "completed", detail)
                self.assertTrue(candidate["gates"][0]["passed"])
            # deterministic ranking: all gates pass, so the smallest diff
            # wins ("edited by fake grok" is the shortest payload)
            self.assertEqual(manifest["selected_candidate"], "grok")
            # integration re-check ran
            self.assertTrue((run_dir / "candidates" / "integration" / "test.log").is_file())

    def test_race_with_all_failures_selects_none(self):
        os.environ["FAKE_PROVIDER_MODE"] = "fail"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            orch = Orchestrator(root, fake_config("false"))
            manifest = orch.race(["grok", "codex"], "doomed", "edit")
            self.assertIsNone(manifest["selected_candidate"])
            for candidate in manifest["candidates"]:
                self.assertEqual(candidate["status"], "failed")


class ReviewWorkflow(unittest.TestCase):
    def test_review_runs_fix_pass_on_findings(self):
        os.environ["FAKE_PROVIDER_MODE"] = "edit"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            orch = Orchestrator(root, fake_config("test -f fake_edit.txt"))
            manifest = orch.review("grok", "claude", "implement and review", "edit")
            self.assertEqual(manifest["selected_candidate"], "grok")
            self.assertEqual(manifest["reviewer"], "claude")
            # the fake reviewer always "finds" something, so a fix pass ran
            self.assertTrue(manifest["review_findings"])
            run_dir = root / ".absoloop" / "runs" / manifest["run_id"]
            self.assertTrue((run_dir / "candidates" / "review-claude" /
                             "final.json").is_file())

    def test_review_requires_distinct_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            orch = Orchestrator(root, fake_config("true"))
            with self.assertRaises(ValueError):
                orch.review("grok", "grok", "x", "edit")


class CouncilWorkflow(unittest.TestCase):
    def test_council_full_pipeline(self):
        os.environ["FAKE_PROVIDER_MODE"] = "edit"
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            orch = Orchestrator(root, fake_config("test -f fake_edit.txt"))
            manifest = orch.council(["grok", "codex"], "council task", "edit")
            self.assertEqual(manifest["strategy"], "council")
            self.assertEqual(manifest["planner"], "claude")
            run_dir = root / ".absoloop" / "runs" / manifest["run_id"]
            self.assertTrue((run_dir / "plan.md").is_file())
            self.assertTrue((run_dir / "summary.md").is_file())
            roles = {c["role"] for c in manifest["candidates"]}
            self.assertEqual(roles, {"grok", "codex"})
            # planner ran read-only: repo root never got the fake edit
            self.assertFalse((root / "fake_edit.txt").exists())


class RankingUnit(unittest.TestCase):
    def _candidate(self, role, passed, diff_size=10, status="completed"):
        from absoloop_harness.core import RunResult
        candidate = Candidate(role=role, provider=role)
        candidate.result = RunResult(run_id="r", provider=role, status=status,
                                     exit_code=0, session=None)
        candidate.gates = [GateResult(name="tests", command="x",
                                      passed=passed, exit_code=0 if passed else 1)]
        return candidate

    def test_gate_survivors_beat_failures(self):
        winner = self._candidate("b", True)
        loser = self._candidate("a", False)
        ranked = Orchestrator.rank_candidates([loser, winner])
        self.assertEqual(ranked[0].role, "b")

    def test_ties_break_alphabetically(self):
        ranked = Orchestrator.rank_candidates(
            [self._candidate("zeta", True), self._candidate("alpha", True)])
        self.assertEqual(ranked[0].role, "alpha")


@unittest.skipUnless(os.environ.get("ABSOLOOP_LIVE_SMOKE") == "1",
                     "live smoke tests are opt-in (ABSOLOOP_LIVE_SMOKE=1)")
class LiveSmoke(unittest.TestCase):
    """Never runs in default CI; requires real, authenticated CLIs and
    per-provider opt-in (ABSOLOOP_SMOKE_CLAUDE=1 etc.)."""

    def _smoke(self, provider: str) -> None:
        if os.environ.get(f"ABSOLOOP_SMOKE_{provider.upper()}") != "1":
            self.skipTest(f"ABSOLOOP_SMOKE_{provider.upper()} not set")
        from absoloop_harness.config import load_config
        from absoloop_harness.core import AgentRequest, EventType
        from absoloop_harness.providers import make_adapter
        cfg = load_config(pathlib.Path.cwd())
        adapter = make_adapter(provider, cfg.get("providers", provider, default={}))
        probe = adapter.probe()
        if not probe.available:
            self.skipTest(f"{provider} CLI not installed")
        with tempfile.TemporaryDirectory() as tmp:
            request = AgentRequest(prompt="Reply with the single word: pong",
                                   cwd=tmp, permission_profile="read",
                                   timeout_seconds=180)
            events = list(adapter.start(request, run_id="smoke"))
        self.assertIn(EventType.RUN_COMPLETED, [e.type for e in events])

    def test_grok(self):
        self._smoke("grok")

    def test_claude(self):
        self._smoke("claude")

    def test_codex(self):
        self._smoke("codex")


if __name__ == "__main__":
    unittest.main()
