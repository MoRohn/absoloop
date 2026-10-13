"""absoloop-workspace: git worktrees, run directories, manifests, patch
export/apply, and cleanup. One writer per worktree — concurrent providers
never share a working tree.

Run layout:
  .absoloop/runs/<run-id>/{manifest.json,events.jsonl,plan.md,summary.md,
                           candidates/<provider>/{final.json,diff.patch,
                                                  test.log,stderr.log}}
  .absoloop/worktrees/<run-id>/<provider-or-role>/
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from .core import AgentEvent, RunResult, redact_text


class WorkspaceError(Exception):
    pass


def _git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        # git.exe shares the parent console by default. A fatal worktree
        # error, or tearing that process down, delivers CTRL_C_EVENT to
        # this console. Detach it so the suite is not KeyboardInterrupt'd.
        from .platform_util import windows_detached_creationflags
        kwargs["creationflags"] = windows_detached_creationflags()
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True, **kwargs)
    if check and result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _is_worktree(path: pathlib.Path) -> bool:
    if not path.is_dir() or not (path / ".git").exists():
        return False
    result = _git(path, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _admin_is_stale(admin: pathlib.Path) -> bool:
    pointer = admin / "gitdir"
    if not pointer.is_file():
        return True
    recorded = pointer.read_text(encoding="utf-8", errors="replace").strip()
    return not pathlib.Path(recorded).exists()


def _link_worktree(repo: pathlib.Path, worktree: pathlib.Path) -> None:
    """Detached linked worktree with an absolute forward-slash gitdir.

    ``git worktree add`` on Windows can leave a gitfile that its own
    checkout cannot reopen (``fatal: not a git repository: .../.git``),
    for a tree nested under the main checkout in ``Temp\\tmp...``. The
    layout matches what ``git worktree add`` writes; the pointer is an
    absolute ``C:/...`` path so backslash escapes never apply.
    """
    repo = repo.resolve()
    worktree = worktree.resolve()
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        raise WorkspaceError(f"{repo} has no .git directory")
    base = git_dir / "worktrees"
    base.mkdir(parents=True, exist_ok=True)
    admin = base / worktree.name
    if admin.exists() and _admin_is_stale(admin):
        shutil.rmtree(admin, ignore_errors=True)
    if admin.exists():
        suffix = 1
        while (base / f"{worktree.name}{suffix}").exists():
            suffix += 1
        admin = base / f"{worktree.name}{suffix}"
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if not head:
        raise WorkspaceError(f"{repo} has no commits to check out")
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "commondir").write_bytes(b"../..\n")
    (admin / "HEAD").write_bytes((head + "\n").encode("ascii"))
    (admin / "gitdir").write_bytes((worktree.as_posix() + "/.git\n").encode("ascii"))
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_bytes(
        ("gitdir: " + admin.as_posix() + "\n").encode("ascii"))
    checkout = _git(worktree, "checkout", "--detach", head, check=False)
    if checkout.returncode != 0:
        raise WorkspaceError(
            "could not check out linked worktree "
            f"{worktree.as_posix()}: {(checkout.stderr or checkout.stdout).strip()}")


def ensure_repo(root: pathlib.Path) -> None:
    if _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise WorkspaceError(f"{root} is not a git repository — worktree "
                             "isolation requires git")


class RunStore:
    """Owns .absoloop/runs/<run-id>/ and .absoloop/worktrees/<run-id>/."""

    def __init__(self, root: pathlib.Path, run_id: str):
        self.root = root.resolve()
        self.run_id = run_id
        self.run_dir = self.root / ".absoloop" / "runs" / run_id
        self.worktrees_dir = self.root / ".absoloop" / "worktrees" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "candidates").mkdir(exist_ok=True)
        self._events_path = self.run_dir / "events.jsonl"
        self._worktrees: Dict[str, pathlib.Path] = {}

    # -- worktrees ------------------------------------------------------------

    def create_worktree(self, role: str) -> pathlib.Path:
        """Detached worktree at HEAD for exactly one writer (provider/role)."""
        ensure_repo(self.root)
        if role in self._worktrees:
            raise WorkspaceError(
                f"worktree for {role!r} already exists in run {self.run_id} — "
                "one writer per worktree is mandatory")
        path = (self.worktrees_dir / role).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Forward slashes. Git for Windows mis-reads a backslash gitdir
        # under AppData\Local\Temp\tmp... (\t becomes a tab).
        added = _git(self.root, "worktree", "add", "--detach", path.as_posix(),
                     check=False)
        if not _is_worktree(path):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            _git(self.root, "worktree", "prune", check=False)
            try:
                _link_worktree(self.root, path)
            except WorkspaceError as exc:
                detail = (added.stderr or added.stdout or "").strip()
                raise WorkspaceError(
                    f"{exc}; git worktree add said: {detail}") from None
        if not _is_worktree(path):
            detail = (added.stderr or added.stdout or "").strip()
            raise WorkspaceError(
                f"git worktree add --detach {path.as_posix()} failed: {detail}")
        self._worktrees[role] = path
        return path

    def export_patch(self, role: str) -> pathlib.Path:
        """Patch of everything the role changed in its worktree (including
        untracked files, via intent-to-add), written to its candidate dir."""
        worktree = self._worktrees.get(role)
        if worktree is None:
            raise WorkspaceError(f"no worktree for {role!r}")
        _git(worktree, "add", "-A", "-N")
        diff = _git(worktree, "diff", "HEAD", "--binary")
        candidate = self.candidate_dir(role)
        patch_path = candidate / "diff.patch"
        patch_path.write_text(diff.stdout, encoding="utf-8")
        return patch_path

    def apply_patch(self, patch_path: pathlib.Path,
                    target: Optional[pathlib.Path] = None) -> None:
        target = target or self.root
        if patch_path.stat().st_size == 0:
            return
        result = _git(target, "apply", "--index", str(patch_path), check=False)
        if result.returncode != 0:
            # --index requires clean staging; retry without it.
            result = _git(target, "apply", str(patch_path), check=False)
        if result.returncode != 0:
            raise WorkspaceError(f"patch {patch_path.name} did not apply: "
                                 f"{result.stderr.strip()}")

    def diff_hash(self, role: str) -> str:
        worktree = self._worktrees.get(role)
        if worktree is None:
            return ""
        import hashlib
        _git(worktree, "add", "-A", "-N")
        diff = _git(worktree, "diff", "HEAD", "--binary")
        return hashlib.sha256(diff.stdout.encode("utf-8")).hexdigest()[:16]

    def cleanup_worktrees(self, keep: bool = False) -> None:
        if keep:
            return
        for role, path in list(self._worktrees.items()):
            _git(self.root, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
            self._worktrees.pop(role, None)
        _git(self.root, "worktree", "prune", check=False)
        shutil.rmtree(self.worktrees_dir, ignore_errors=True)
        try:  # drop the shared parent when this was the last run using it
            self.worktrees_dir.parent.rmdir()
        except OSError:
            pass

    def adopt_worktree(self, role: str, path: pathlib.Path) -> None:
        """Register an existing directory (e.g. the repo root for `single`
        with no isolation) as the role's writing surface."""
        self._worktrees[role] = path

    def worktree(self, role: str) -> Optional[pathlib.Path]:
        return self._worktrees.get(role)

    # -- artifacts ------------------------------------------------------------

    def candidate_dir(self, provider_or_role: str) -> pathlib.Path:
        path = self.run_dir / "candidates" / provider_or_role
        path.mkdir(parents=True, exist_ok=True)
        return path

    def append_event(self, event: AgentEvent) -> None:
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")

    def write_candidate_result(self, role: str, result: RunResult) -> None:
        path = self.candidate_dir(role) / "final.json"
        path.write_text(json.dumps(result.to_json(), indent=2) + "\n", encoding="utf-8")

    def write_stderr(self, role: str, stderr: str) -> None:
        (self.candidate_dir(role) / "stderr.log").write_text(
            redact_text(stderr or ""), encoding="utf-8")

    def write_test_log(self, role: str, log: str) -> None:
        (self.candidate_dir(role) / "test.log").write_text(
            redact_text(log or ""), encoding="utf-8")

    def write_plan(self, plan: str) -> None:
        (self.run_dir / "plan.md").write_text(plan, encoding="utf-8")

    def write_summary(self, summary: str) -> None:
        (self.run_dir / "summary.md").write_text(summary, encoding="utf-8")

    # -- manifest -------------------------------------------------------------

    def write_manifest(self, manifest: Dict[str, Any]) -> pathlib.Path:
        """The run manifest. Contains provider/version/capabilities,
        timestamps, session ids, prompt hash, permission profile, exit
        status, usage, artifact paths, and diff hashes — never credentials
        or raw environment dumps."""
        manifest = dict(manifest)
        manifest.setdefault("run_id", self.run_id)
        manifest.setdefault("written_at", time.time())
        path = self.run_dir / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False,
                                   default=str) + "\n", encoding="utf-8")
        return path

    def read_manifest(self) -> Dict[str, Any]:
        path = self.run_dir / "manifest.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


def list_runs(root: pathlib.Path) -> List[str]:
    runs_dir = root / ".absoloop" / "runs"
    if not runs_dir.is_dir():
        return []
    return sorted((p.name for p in runs_dir.iterdir() if p.is_dir()), reverse=True)


def prune_runs(root: pathlib.Path, retention: int) -> None:
    runs = list_runs(root)
    for stale in runs[retention:]:
        shutil.rmtree(root / ".absoloop" / "runs" / stale, ignore_errors=True)
