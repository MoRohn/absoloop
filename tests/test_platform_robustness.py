"""Cross-platform + Codex Micro robustness (Linux / macOS / Windows)."""
from __future__ import annotations

import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from absoloop_harness import platform_util as pu
from absoloop_harness import shortcuts as sc
from absoloop_harness.providers.claude import ClaudeAdapter
from absoloop_harness.providers.codex import CodexAdapter
from absoloop_harness.providers.grok import GrokAdapter


class ToolingHome(unittest.TestCase):
    def test_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "absoloop_harness").mkdir()
            got = pu.tooling_home({"ABSOLOOP_HOME": str(root)})
            self.assertEqual(got, root.resolve())

    def test_anchor_detection(self):
        repo = pathlib.Path(__file__).resolve().parent.parent
        got = pu.tooling_home({}, anchor=repo)
        self.assertEqual(got, repo)


class PythonGateRewrite(unittest.TestCase):
    def test_rewrites_python3(self):
        out = pu.rewrite_python_gate("python3 -m unittest discover -s tests")
        self.assertTrue(out.startswith(sys.executable) or out.startswith(f'"{sys.executable}"'))
        self.assertIn("-m unittest", out)

    def test_rewrites_python(self):
        out = pu.rewrite_python_gate("python -m pytest")
        self.assertIn("-m pytest", out)
        self.assertTrue(
            out.startswith(sys.executable) or out.startswith(f'"{sys.executable}"'))

    def test_leaves_npm(self):
        self.assertEqual(pu.rewrite_python_gate("npm test"), "npm test")


class ResolveExecutable(unittest.TestCase):
    def test_resolves_python(self):
        path = pu.resolve_executable("python3") or pu.resolve_executable("python")
        self.assertIsNotNone(path)
        self.assertTrue(pathlib.Path(path).exists())

    def test_missing(self):
        self.assertIsNone(pu.resolve_executable("absoloop-no-such-binary-xyz"))


class StdioGuard(unittest.TestCase):
    def test_ensure_stdio_replaces_unencodable_characters(self):
        stream = mock.Mock()
        with mock.patch.object(pu.sys, "stdout", stream), \
             mock.patch.object(pu.sys, "stderr", stream), \
             mock.patch.object(pu, "_stdio_ready", False):
            pu.ensure_stdio()
            pu.ensure_stdio()
        stream.reconfigure.assert_called_with(errors="replace")
        self.assertEqual(stream.reconfigure.call_count, 2)


class KillTree(unittest.TestCase):
    def test_windows_taskkill_does_not_attach_a_console(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(pu, "is_windows", return_value=True), \
             mock.patch.object(pu.subprocess, "run", return_value=completed) as run:
            self.assertTrue(pu.kill_process_tree(4321))
        args = run.call_args.args[0]
        self.assertEqual(args[:5], ["taskkill", "/PID", "4321", "/T", "/F"])
        flags = run.call_args.kwargs.get("creationflags", 0)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)


class SpawnArgv(unittest.TestCase):
    def test_windows_python_script_uses_the_running_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = pathlib.Path(tmp) / "fake_provider.py"
            script.write_text("#!/usr/bin/env python3\nprint('9.9.9')\n", encoding="utf-8")
            with mock.patch.object(pu, "is_windows", return_value=True):
                argv = pu.prepare_spawn_argv([str(script), "--version"])
            self.assertEqual(argv[0], sys.executable)
            self.assertEqual(argv[1], str(script))
            self.assertEqual(argv[2], "--version")

    def test_windows_exe_is_not_rewritten(self):
        with mock.patch.object(pu, "is_windows", return_value=True):
            argv = pu.prepare_spawn_argv([r"C:\Tools\codex.exe", "exec"])
        self.assertEqual(argv, [r"C:\Tools\codex.exe", "exec"])

    def test_posix_argv_is_unchanged(self):
        with mock.patch.object(pu, "is_windows", return_value=False):
            argv = pu.prepare_spawn_argv(["/tmp/fake_provider.py", "--version"])
        self.assertEqual(argv, ["/tmp/fake_provider.py", "--version"])


class FuncKeyDecode(unittest.TestCase):
    def test_f13_through_f24(self):
        # xterm CSI numbers used by Micro defaults
        mapping = {
            "25": "f13", "26": "f14", "28": "f15", "29": "f16",
            "31": "f17", "32": "f18", "33": "f19", "34": "f20",
            "42": "f21", "43": "f22", "44": "f23", "45": "f24",
        }
        for code, name in mapping.items():
            self.assertEqual(sc._decode_escape(f"\x1b[{code}~"), name)

    def test_default_bindings_cover_f21_f24(self):
        cfg = sc.ShortcutConfig(bindings=dict(sc.DEFAULT_BINDINGS))
        self.assertEqual(cfg.action_for_chord("f21"), "cancel")
        self.assertEqual(cfg.action_for_chord("f22"), "doctor")
        self.assertEqual(cfg.action_for_chord("f23"), "inspect")
        self.assertEqual(cfg.action_for_chord("f24"), "run")


class ListenFallback(unittest.TestCase):
    def test_windows_falls_back_to_line_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            stdin = io.StringIO("action:status\n")
            with mock.patch.object(sc.sys, "stdin", stdin), \
                 mock.patch.object(sc.sys.stdin, "isatty", return_value=True), \
                 mock.patch("absoloop_harness.platform_util.tty_raw_listen_supported",
                            return_value=False), \
                 mock.patch.object(sc, "run_action", return_value=0) as run:
                code = sc.listen(root, once=True)
            self.assertEqual(code, 0)
            run.assert_called()
            self.assertEqual(run.call_args[0][0], "status")


class AuthHints(unittest.TestCase):
    def test_grok_hint_without_creds(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            hint = GrokAdapter().auth_hint()
            self.assertIn("grok login", hint)

    def test_claude_hint_mentions_login(self):
        hint = ClaudeAdapter().auth_hint()
        self.assertTrue("claude" in hint.lower() or "ANTHROPIC" in hint)

    def test_codex_hint_mentions_login(self):
        hint = CodexAdapter().auth_hint()
        self.assertTrue("codex" in hint.lower() or "OPENAI" in hint)


class RequireExecutable(unittest.TestCase):
    def test_missing_raises(self):
        from absoloop_harness.core import PermissionMappingError
        adapter = GrokAdapter({"command": "absoloop-no-such-cli-zzz"})
        with self.assertRaises(PermissionMappingError):
            adapter.require_executable()


class Prerequisites(unittest.TestCase):
    def test_includes_home_and_platform(self):
        notes = pu.prerequisite_checks()
        joined = "\n".join(notes)
        self.assertIn("ABSOLOOP_HOME=", joined)
        self.assertIn("python=", joined)


if __name__ == "__main__":
    unittest.main()
