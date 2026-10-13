"""Worktree isolation, patch export/apply, manifests, and run pruning."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from absoloop_harness import workspace as ws
from absoloop_harness.core import AgentEvent, EventType
from absoloop_harness.workspace import (RunStore, WorkspaceError, list_runs,
                                        prune_runs)


def make_repo(root: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)


class Worktrees(unittest.TestCase):
    def test_isolation_and_patch_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            store = RunStore(root, "run-1")
            wt_a = store.create_worktree("prov-a")
            wt_b = store.create_worktree("prov-b")
            self.assertNotEqual(wt_a, wt_b)

            (wt_a / "hello.txt").write_text("changed by a\n", encoding="utf-8")
            (wt_a / "new_a.txt").write_text("brand new\n", encoding="utf-8")
            (wt_b / "hello.txt").write_text("changed by b\n", encoding="utf-8")

            # Isolation: neither tree sees the other's edits, root untouched.
            self.assertEqual((root / "hello.txt").read_text(), "hello\n")
            self.assertEqual((wt_b / "hello.txt").read_text(), "changed by b\n")
            self.assertFalse((wt_b / "new_a.txt").exists())

            patch = store.export_patch("prov-a")
            self.assertGreater(patch.stat().st_size, 0)
            self.assertTrue(store.diff_hash("prov-a"))
            self.assertNotEqual(store.diff_hash("prov-a"), store.diff_hash("prov-b"))

            store.apply_patch(patch)   # to the root repo
            self.assertEqual((root / "hello.txt").read_text(), "changed by a\n")
            self.assertEqual((root / "new_a.txt").read_text(), "brand new\n")

            store.cleanup_worktrees()
            self.assertFalse(wt_a.exists())
            self.assertFalse(wt_b.exists())

    def test_second_writer_for_same_role_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            store = RunStore(root, "run-2")
            store.create_worktree("solo")
            with self.assertRaises(WorkspaceError):
                store.create_worktree("solo")
            store.cleanup_worktrees()

    def test_relinks_when_git_worktree_add_cannot_reopen_the_gitfile(self):
        """Git for Windows can reject the gitfile it just wrote.

        The CI failure is ``fatal: not a git repository: .../grok/.git``
        right after ``Preparing worktree``. The run must still get a real
        detached worktree, with an absolute forward-slash gitdir.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_repo(root)
            store = RunStore(root, "run-relink")
            real = ws._git

            def flaky(repo, *args, check=True):
                if len(args) >= 2 and args[0] == "worktree" and args[1] == "add":
                    return subprocess.CompletedProcess(
                        ["git", *args], 128, "",
                        "Preparing worktree (detached HEAD deadbeef)\n"
                        "fatal: not a git repository: 'C:/Temp/example/.git'")
                return real(repo, *args, check=check)

            with mock.patch.object(ws, "_git", side_effect=flaky):
                wt = store.create_worktree("grok")
            self.assertEqual((wt / "hello.txt").read_text(encoding="utf-8"), "hello\n")
            gitfile = (wt / ".git").read_text(encoding="utf-8")
            self.assertTrue(gitfile.startswith("gitdir: "))
            self.assertNotIn("\\", gitfile)
            self.assertTrue(gitfile.strip().endswith("/.git/worktrees/grok"))
            store.cleanup_worktrees()
            self.assertFalse(wt.exists())

    def test_non_git_dir_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(pathlib.Path(tmp), "run-3")
            with self.assertRaises(WorkspaceError):
                store.create_worktree("x")


class Manifests(unittest.TestCase):
    def test_manifest_roundtrip_and_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            store = RunStore(root, "20260101-000000-aaa")
            store.write_manifest({"strategy": "single", "selected_candidate": "grok"})
            manifest = store.read_manifest()
            self.assertEqual(manifest["strategy"], "single")
            self.assertEqual(manifest["run_id"], "20260101-000000-aaa")
            self.assertIn("20260101-000000-aaa", list_runs(root))

    def test_parallel_event_appends_stay_one_json_object_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            store = RunStore(root, "run-events")

            def write(lane: int) -> None:
                for n in range(25):
                    store.append_event(AgentEvent(
                        type=EventType.TEXT_DELTA, provider="p",
                        text=f"{lane}-{n}"))

            threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            lines = (store.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 100)
            texts = []
            for line in lines:
                texts.append(json.loads(line)["text"])
            self.assertEqual(len(texts), len(set(texts)))

    def test_prune_keeps_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for index in range(5):
                RunStore(root, f"2026010{index}-000000-x").write_manifest({})
            prune_runs(root, retention=2)
            self.assertEqual(len(list_runs(root)), 2)
            self.assertIn("20260104-000000-x", list_runs(root))


if __name__ == "__main__":
    unittest.main()
