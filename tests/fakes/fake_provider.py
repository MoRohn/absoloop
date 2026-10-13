#!/usr/bin/env python3
"""Fake provider executable for integration tests.

Auto-detects which provider it is impersonating from the argv shape the
adapter built (codex: `exec` subcommand; grok: `--output-format
streaming-json`; claude: `--output-format stream-json`), so one binary can
serve all three adapters — including in parallel workflows.

Misbehavior is selected with FAKE_PROVIDER_MODE:
  success       normal streaming run (default)
  partial-json  emits a truncated JSON line mid-stream
  unknown       emits unrecognized event types
  stderr-noise  chats on stderr while streaming valid stdout
  fail          exits non-zero after an error event
  hang          streams one event then sleeps forever (timeout/cancel tests)
  edit          writes FAKE_PROVIDER_EDIT_FILE into the cwd — but only when
                the argv carries a write-capable permission mapping
  leak          includes a fake secret in its output (redaction tests)
"""
import json
import os
import pathlib
import sys
import time

MODE = os.environ.get("FAKE_PROVIDER_MODE", "success")
SESSION = os.environ.get("FAKE_PROVIDER_SESSION", "fake-session-123")

_WRITE_MARKERS = {"--allow", "--yolo", "acceptEdits", "bypassPermissions",
                  "workspace-write", "danger-full-access"}


def detect_style(argv):
    if "exec" in argv[:3]:
        return "codex"
    if "streaming-json" in argv:
        return "grok"
    return "claude"


def can_write(argv):
    return bool(_WRITE_MARKERS.intersection(argv))


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def hang() -> int:
    """Block until the parent cancels this process.

    ``time.sleep(3600)`` does not return when Windows delivers CTRL_BREAK
    to the process group, so the parent would have to hard-kill every
    hang. Short sleeps let SIGBREAK become KeyboardInterrupt. SIGTERM on
    Unix still ends the process with the default disposition.
    """
    if os.name == "nt":
        import signal
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            try:
                signal.signal(sigbreak, signal.default_int_handler)
            except (OSError, ValueError):
                pass
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 130


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print("fake-provider 9.9.9")
        return 0
    if "--help" in argv or "-h" in argv or argv == ["version"]:
        # Probe helpers (`codex exec resume --help`, etc.) must not hang on
        # stdin or emit a full event stream.
        print("fake-provider 9.9.9")
        print("Usage: fake-provider [exec] [resume] [--json] [--sandbox]")
        return 0
    style = detect_style(argv)

    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except OSError:
            pass

    if MODE == "stderr-noise":
        print("warning: update available", file=sys.stderr)
        print("diagnostic chatter that must stay off stdout", file=sys.stderr)

    if MODE == "edit" and can_write(argv):
        target = os.environ.get("FAKE_PROVIDER_EDIT_FILE", "fake_edit.txt")
        pathlib.Path(target).write_text(f"edited by fake {style}\n", encoding="utf-8")

    leak = "xai-supersecretapikey1234567890" if MODE == "leak" else ""

    if style == "grok":
        emit({"type": "text", "data": "working on it"})
        if MODE == "hang":
            return hang()
        if MODE == "partial-json":
            sys.stdout.write('{"type":"text","data":"trunc\n')
            sys.stdout.flush()
        if MODE == "unknown":
            emit({"type": "auto_compact_started", "detail": "x"})
        emit({"type": "thought", "data": "thinking summary"})
        if leak:
            emit({"type": "text", "data": f"using key {leak}"})
        if MODE == "fail":
            emit({"type": "error", "message": "engine exploded"})
            return 1
        emit({"type": "end", "stopReason": "EndTurn", "sessionId": SESSION,
              "usage": {"input_tokens": 10, "output_tokens": 5},
              "num_turns": 1, "total_cost_usd": 0.01})
        return 0

    if style == "claude":
        emit({"type": "system", "subtype": "init", "session_id": SESSION,
              "model": "fake-claude"})
        if MODE == "hang":
            return hang()
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit",
             "input": {"file_path": "a.py"}},
            {"type": "text", "text": "done" + (f" key {leak}" if leak else "")}]}})
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": False}]}})
        if MODE == "unknown":
            emit({"type": "compact_boundary", "detail": "x"})
        if MODE == "fail":
            emit({"type": "result", "subtype": "error_during_execution",
                  "is_error": True, "session_id": SESSION, "result": "boom"})
            return 1
        emit({"type": "result", "subtype": "success", "is_error": False,
              "session_id": SESSION, "result": "all done",
              "usage": {"input_tokens": 12, "output_tokens": 4},
              "num_turns": 2, "total_cost_usd": 0.02})
        return 0

    # codex
    emit({"type": "thread.started", "thread_id": SESSION})
    if MODE == "hang":
        return hang()
    emit({"type": "item.started", "item": {"item_type": "command_execution",
                                           "command": "pytest"}})
    emit({"type": "item.completed", "item": {"item_type": "command_execution",
                                             "command": "pytest", "exit_code": 0}})
    emit({"type": "item.completed", "item": {"item_type": "file_change",
                                             "changes": [{"path": "b.py"}]}})
    if MODE == "unknown":
        emit({"type": "totally.new.event", "x": 1})
    emit({"type": "item.completed", "item": {
        "item_type": "agent_message",
        "text": "finished" + (f" key {leak}" if leak else "")}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 3}})
    if MODE == "fail":
        emit({"type": "error", "message": "codex failed"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
