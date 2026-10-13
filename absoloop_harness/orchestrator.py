"""absoloop-orchestrator: single, review, race, and council workflows over
provider adapters, with deterministic gates deciding candidate selection.

Discipline (non-negotiable):
- one writer per worktree;
- deterministic gates (repo-native tests/lint/typecheck) run before any
  LLM judgment; failing a mandatory gate rejects the candidate outright;
- a cross-provider reviewer runs only after gates; an LLM judge is only a
  tie-breaker;
- every run leaves reproducible artifacts under .absoloop/runs/<run-id>/.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config
from .core import (AgentEvent, AgentRequest, EventType, RunResult,
                   SessionRef, new_run_id)
from .delegation import with_delegation
from .providers import make_adapter
from .providers.base import ProviderAdapter
from . import runtime as run_ctrl
from .workspace import RunStore, prune_runs


@dataclass
class GateResult:
    name: str
    command: str
    passed: bool
    exit_code: Optional[int]
    log: str = ""


@dataclass
class Candidate:
    role: str                          # provider name or "<provider>-2" style role
    provider: str
    result: Optional[RunResult] = None
    gates: List[GateResult] = field(default_factory=list)
    patch_path: Optional[pathlib.Path] = None
    diff_hash: str = ""

    @property
    def gates_passed(self) -> bool:
        # Empty gates used to vacuously pass — that let crashed race lanes
        # (result=None / not_run) beat real candidates. Require a completed
        # result; an empty gate list then means "no gates configured".
        if self.result is None or self.result.status != "completed":
            return False
        return all(gate.passed for gate in self.gates)

    @property
    def diff_size(self) -> int:
        try:
            return self.patch_path.stat().st_size if self.patch_path else 0
        except OSError:
            return 0


class Orchestrator:
    """Owns harness workflows: single, review, race, and council.

    Each workflow creates a ``RunStore``, drives one or more adapters via
    ``_drive``, runs deterministic gates, ranks candidates, and finalizes
    ``live.json`` / the manifest. Callers never share worktrees across writers.
    """

    def __init__(self, root: pathlib.Path, cfg: Config,
                 on_event=None, keep_worktrees: Optional[bool] = None):
        self.root = root.resolve()
        self.cfg = cfg
        self.on_event = on_event or (lambda event: None)
        keep_cfg = bool(cfg.get("artifacts", "keep_worktrees", default=False))
        self.keep_worktrees = keep_cfg if keep_worktrees is None else keep_worktrees

    # -- provider plumbing -----------------------------------------------------

    def adapter(self, provider: str) -> ProviderAdapter:
        """Construct a configured adapter; probe/start happen at call sites."""
        return make_adapter(provider, self.cfg.get("providers", provider, default={}))

    def _drive(self, adapter: ProviderAdapter, request: AgentRequest,
               store: RunStore, role: str,
               resume: Optional[SessionRef] = None) -> RunResult:
        """Run one provider to completion, streaming events into the run
        store and returning a normalized RunResult."""
        adapter.bind_run(store.run_dir, role)
        events_count = 0
        final_text_parts: List[str] = []
        usage: Dict[str, Any] = {}
        cost: Optional[float] = None
        status = "failed"
        started = time.time()
        stream = (adapter.resume(resume, request, run_id=store.run_id) if resume
                  else adapter.start(request, run_id=store.run_id))
        for event in stream:
            events_count += 1
            store.append_event(event)
            self.on_event(event)
            if event.type == EventType.TEXT_DELTA:
                final_text_parts.append(event.text)
            elif event.type == EventType.USAGE:
                usage = event.data.get("usage") or usage
                if event.data.get("total_cost_usd") is not None:
                    cost = event.data["total_cost_usd"]
            elif event.type == EventType.RUN_COMPLETED:
                status = "completed"
            elif event.type == EventType.RUN_FAILED:
                if event.data.get("cancelled"):
                    status = "cancelled"
                elif event.data.get("timed_out"):
                    status = "timeout"
                else:
                    status = "failed"
        if run_ctrl.cancel_requested(store.run_dir):
            status = "cancelled"
        outcome = adapter.last_outcome
        result = RunResult(
            run_id=store.run_id, provider=adapter.name, status=status,
            exit_code=outcome.exit_code if outcome else None,
            session=adapter.last_session,
            final_text="".join(final_text_parts)[-20000:],
            usage=usage if isinstance(usage, dict) else {},
            cost_usd=cost,
            duration_seconds=time.time() - started,
            events_count=events_count)
        store.write_candidate_result(role, result)
        if outcome:
            store.write_stderr(role, outcome.stderr)
        return result

    def _begin(self, store: RunStore, strategy: str,
               providers: List[str], prompt: str, profile: str) -> None:
        run_ctrl.begin_run(store.run_dir, run_id=store.run_id,
                           strategy=strategy, providers=providers)
        # Partial manifest so inspect/cancel work while the run is live.
        store.write_manifest({
            "run_id": store.run_id,
            "strategy": strategy,
            "status": "running",
            "permission_profile": profile,
            "prompt_hash": __import__("hashlib").sha256(
                prompt.encode("utf-8")).hexdigest()[:16],
            "selected_candidate": None,
            "candidates": [],
            "live": True,
        })

    def _finish(self, store: RunStore, status: str) -> None:
        run_ctrl.finish_run(store.run_dir, status)
        # Out-of-CLI chime when a harness run settles (never blocks).
        try:
            from .notify import notify
            kind = "done" if status == "completed" else (
                "fail" if status in ("failed", "cancelled", "timeout") else "attention")
            notify("AbsoLoop", f"harness {store.run_id}: {status}", kind=kind)
        except Exception:
            pass

    # -- deterministic gates ---------------------------------------------------

    def run_gates(self, workdir: pathlib.Path, store: RunStore,
                  role: str) -> List[GateResult]:
        required = [str(name) for name in
                    self.cfg.get("gates", "required", default=["tests"])]
        commands = self.cfg.get("gates", "commands", default={})
        results: List[GateResult] = []
        logs: List[str] = []
        for name in required:
            command = str(commands.get(name, "") or "")
            if not command:
                continue
            # Gate commands come from trusted config, never from prompts;
            # they are the one legitimate shell=True surface (repo-native
            # invocations like "npm test -- --ci" need shell semantics).
            # Rewrite python/python3 → current interpreter so Windows OOTB
            # works when only `py`/`python` exist.
            from .platform_util import rewrite_python_gate
            command = rewrite_python_gate(command)
            proc = subprocess.run(command, shell=True, cwd=str(workdir),
                                  capture_output=True, text=True, timeout=1800)
            passed = proc.returncode == 0
            log = (proc.stdout or "") + (proc.stderr or "")
            results.append(GateResult(name=name, command=command, passed=passed,
                                      exit_code=proc.returncode, log=log[-10000:]))
            logs.append(f"=== gate {name}: {'PASS' if passed else 'FAIL'} "
                        f"(exit {proc.returncode}) ===\n{log[-10000:]}")
        store.write_test_log(role, "\n".join(logs))
        return results

    # -- selection --------------------------------------------------------------

    @staticmethod
    def rank_candidates(candidates: List[Candidate]) -> List[Candidate]:
        """Deterministic ranking: gate survivors first, then fewest gate
        failures, then completed runs, then smallest diff. Stable order makes
        selection reproducible; any LLM judge is applied by the caller only
        to break exact ties."""
        def key(candidate: Candidate):
            failures = sum(1 for gate in candidate.gates if not gate.passed)
            completed = 0 if (candidate.result and candidate.result.status == "completed") else 1
            return (0 if candidate.gates_passed else 1, failures, completed,
                    candidate.diff_size, candidate.role)
        return sorted(candidates, key=key)

    # -- workflows ---------------------------------------------------------------

    def single(self, provider: str, prompt: str, profile: str,
               isolate: bool = True, run_id: Optional[str] = None) -> Dict[str, Any]:
        run_id = run_id or new_run_id()
        store = RunStore(self.root, run_id)
        adapter = self.adapter(provider)
        probe = adapter.probe()
        if not probe.available:
            raise RuntimeError("; ".join(probe.problems))
        self._begin(store, "single", [provider], prompt, profile)
        final_status = "failed"
        try:
            workdir = store.create_worktree(provider) if isolate else self.root
            if not isolate:
                store.adopt_worktree(provider, self.root)
            request = self._request(prompt, workdir, profile, provider)
            result = self._drive(adapter, request, store, provider)
            candidate = Candidate(role=provider, provider=provider, result=result)
            if result.status != "cancelled":
                candidate.gates = self.run_gates(workdir, store, provider)
                candidate.patch_path = store.export_patch(provider) if isolate else None
                candidate.diff_hash = store.diff_hash(provider)
            selected = (provider if candidate.gates_passed
                        and result.status == "completed" else None)
            final_status = result.status
            manifest = self._manifest(store, "single", [candidate], [probe],
                                      prompt, profile, selected=selected,
                                      status=final_status)
            store.write_summary(self._summary("single", [candidate], selected))
            store.write_manifest(manifest)
            return manifest
        finally:
            self._finish(store, final_status)
            store.cleanup_worktrees(keep=self.keep_worktrees)
            self._prune()

    def review(self, implementer: str, reviewer: str, prompt: str,
               profile: str, run_id: Optional[str] = None) -> Dict[str, Any]:
        if implementer == reviewer:
            raise ValueError("review requires two different providers")
        run_id = run_id or new_run_id()
        store = RunStore(self.root, run_id)
        impl = self.adapter(implementer)
        rev = self.adapter(reviewer)
        probes = [impl.probe(), rev.probe()]
        for probe in probes:
            if not probe.available:
                raise RuntimeError("; ".join(probe.problems))
        self._begin(store, "review", [implementer, reviewer], prompt, profile)
        final_status = "failed"
        findings = ""
        try:
            workdir = store.create_worktree(implementer)
            request = self._request(prompt, workdir, profile, implementer)
            result = self._drive(impl, request, store, implementer)
            if result.status == "cancelled" or run_ctrl.cancel_requested(store.run_dir):
                final_status = "cancelled"
                candidate = Candidate(role=implementer, provider=implementer,
                                      result=result)
                manifest = self._manifest(store, "review", [candidate], probes,
                                          prompt, profile, selected=None,
                                          status="cancelled",
                                          extra={"reviewer": reviewer})
                store.write_summary(self._summary("review", [candidate], None))
                store.write_manifest(manifest)
                return manifest
            store.export_patch(implementer)

            # Reviewer inspects the same worktree read-only — it is not a
            # second writer, it only reads and reports.
            review_prompt = (
                "You are reviewing another agent's uncommitted changes in this "
                "working tree (use `git diff HEAD`).\n"
                f"The original task was:\n{prompt}\n\n"
                "List concrete, verifiable problems (bugs, missed requirements, "
                "weakened tests). If the change is sound, answer exactly: "
                "NO_BLOCKING_FINDINGS.")
            review_request = self._request(review_prompt, workdir, "read", reviewer)
            review_result = self._drive(rev, review_request, store, f"review-{reviewer}")

            findings = review_result.final_text.strip()
            if findings and "NO_BLOCKING_FINDINGS" not in findings:
                fix_prompt = (f"A independent reviewer found these issues with your "
                              f"changes for the task:\n{prompt}\n\nFindings:\n"
                              f"{findings[:8000]}\n\nFix the verified findings. "
                              "Do not weaken tests.")
                fix_request = self._request(fix_prompt, workdir, profile, implementer)
                resume = result.session
                result = self._drive(impl, fix_request, store, implementer,
                                     resume=resume)
            candidate = Candidate(role=implementer, provider=implementer, result=result)
            candidate.gates = self.run_gates(workdir, store, implementer)
            candidate.patch_path = store.export_patch(implementer)
            candidate.diff_hash = store.diff_hash(implementer)
            selected = (implementer if candidate.gates_passed
                        and result.status == "completed" else None)
            final_status = ("cancelled" if result.status == "cancelled"
                            else ("completed" if selected else "failed"))
            manifest = self._manifest(store, "review", [candidate], probes,
                                      prompt, profile, selected=selected,
                                      status=final_status,
                                      extra={"reviewer": reviewer,
                                             "review_findings": findings[:4000]})
            store.write_summary(self._summary("review", [candidate], selected))
            store.write_manifest(manifest)
            return manifest
        finally:
            self._finish(store, final_status)
            store.cleanup_worktrees(keep=self.keep_worktrees)
            self._prune()

    def race(self, providers: List[str], prompt: str, profile: str,
             run_id: Optional[str] = None) -> Dict[str, Any]:
        run_id = run_id or new_run_id()
        store = RunStore(self.root, run_id)
        probes = []
        adapters: Dict[str, ProviderAdapter] = {}
        for provider in providers:
            adapter = self.adapter(provider)
            probe = adapter.probe()
            if not probe.available:
                raise RuntimeError("; ".join(probe.problems))
            adapters[provider] = adapter
            probes.append(probe)
        self._begin(store, "race", providers, prompt, profile)
        final_status = "failed"
        try:
            candidates: List[Candidate] = []
            import threading
            lock = threading.Lock()

            # Worktree creation mutates shared git state — pre-create, then
            # run providers in parallel against their own trees.
            workdirs = {provider: store.create_worktree(provider)
                        for provider in providers}

            def run_one(provider: str) -> None:
                try:
                    if run_ctrl.cancel_requested(store.run_dir):
                        return
                    request = self._request(prompt, workdirs[provider],
                                            profile, provider)
                    result = self._drive(adapters[provider], request, store,
                                         provider)
                    candidate = Candidate(role=provider, provider=provider,
                                          result=result)
                    if result.status != "cancelled":
                        candidate.gates = self.run_gates(
                            workdirs[provider], store, provider)
                        candidate.patch_path = store.export_patch(provider)
                        candidate.diff_hash = store.diff_hash(provider)
                    with lock:
                        candidates.append(candidate)
                except Exception as exc:  # noqa: BLE001 — surface in artifacts
                    import traceback
                    with lock:
                        failed = Candidate(role=provider, provider=provider)
                        store.write_stderr(
                            provider,
                            f"race lane crashed: {exc}\n{traceback.format_exc()}")
                        candidates.append(failed)

            threads = [threading.Thread(target=run_one, args=(p,)) for p in providers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            if run_ctrl.cancel_requested(store.run_dir) or any(
                    c.result and c.result.status == "cancelled" for c in candidates):
                final_status = "cancelled"
                ranked = self.rank_candidates(candidates)
                manifest = self._manifest(store, "race", candidates, probes,
                                          prompt, profile, selected=None,
                                          status="cancelled")
                store.write_summary(self._summary("race", ranked, None))
                store.write_manifest(manifest)
                return manifest

            ranked = self.rank_candidates(candidates)
            winner = ranked[0] if ranked and ranked[0].gates_passed else None
            selected = winner.role if winner else None
            if winner and winner.patch_path:
                integration = store.create_worktree("integration")
                store.apply_patch(winner.patch_path, target=integration)
                integration_gates = self.run_gates(integration, store, "integration")
                if not all(g.passed for g in integration_gates):
                    selected = None   # winner did not survive integration re-check
            final_status = "completed" if selected else "failed"
            manifest = self._manifest(store, "race", candidates, probes,
                                      prompt, profile, selected=selected,
                                      status=final_status)
            store.write_summary(self._summary("race", ranked, selected))
            store.write_manifest(manifest)
            return manifest
        finally:
            self._finish(store, final_status)
            store.cleanup_worktrees(keep=self.keep_worktrees)
            self._prune()

    def council(self, providers: List[str], prompt: str, profile: str,
                run_id: Optional[str] = None) -> Dict[str, Any]:
        """planner -> parallel implementers -> reviewer/verifier -> integrator."""
        run_id = run_id or new_run_id()
        store = RunStore(self.root, run_id)
        planner_name = str(self.cfg.get("workflows", "planner", default=providers[0]))
        reviewer_name = str(self.cfg.get("workflows", "reviewer", default=providers[-1]))
        self._begin(store, "council",
                    list(dict.fromkeys([planner_name, *providers, reviewer_name])),
                    prompt, profile)
        final_status = "failed"
        try:
            # 1. Plan (read-only, in the repo root — the planner writes nothing).
            planner = self.adapter(planner_name)
            plan_prompt = (
                f"Produce a concise implementation plan with explicit, testable "
                f"acceptance criteria for this task. Plan only — change nothing.\n\n"
                f"Task:\n{prompt}")
            plan_request = self._request(plan_prompt, self.root, "read", planner_name)
            plan_result = self._drive(planner, plan_request, store, f"plan-{planner_name}")
            plan = plan_result.final_text or prompt
            store.write_plan(plan)

            # 2-3. Parallel implementers in isolated worktrees, guided by the plan.
            impl_prompt = (f"Implement this task following the plan and its "
                           f"acceptance criteria.\n\nTask:\n{prompt}\n\nPlan:\n{plan[:8000]}")
            race_manifest = self._council_implement(store, providers, impl_prompt, profile)
            candidates: List[Candidate] = race_manifest["_candidates"]

            # 4-6. Gates already ran per candidate; reviewer verifies the leader.
            ranked = self.rank_candidates(candidates)
            winner = ranked[0] if ranked and ranked[0].gates_passed else None
            selected = winner.role if winner else None
            review_findings = ""
            if winner is not None:
                reviewer = self.adapter(reviewer_name)
                worktree = store.worktree(winner.role)
                review_prompt = (
                    "Verify this working tree against the plan's acceptance "
                    f"criteria (use `git diff HEAD`).\n\nPlan:\n{plan[:6000]}\n\n"
                    "Answer exactly NO_BLOCKING_FINDINGS if acceptable, else list "
                    "blocking findings.")
                review_request = self._request(review_prompt, worktree or self.root,
                                               "read", reviewer_name)
                review_result = self._drive(reviewer, review_request, store,
                                            f"verify-{reviewer_name}")
                review_findings = review_result.final_text.strip()
                if review_findings and "NO_BLOCKING_FINDINGS" not in review_findings:
                    selected = None

            # 7-8. Integrate the winner and re-run all mandatory gates.
            if selected and winner and winner.patch_path:
                integration = store.create_worktree("integration")
                store.apply_patch(winner.patch_path, target=integration)
                integration_gates = self.run_gates(integration, store, "integration")
                if not all(g.passed for g in integration_gates):
                    selected = None

            if run_ctrl.cancel_requested(store.run_dir) or plan_result.status == "cancelled":
                selected = None
                final_status = "cancelled"
            else:
                final_status = "completed" if selected else "failed"
            probes = [self.adapter(p).probe() for p in providers]
            manifest = self._manifest(store, "council", candidates, probes,
                                      prompt, profile, selected=selected,
                                      status=final_status,
                                      extra={"planner": planner_name,
                                             "reviewer": reviewer_name,
                                             "review_findings": review_findings[:4000]})
            store.write_summary(self._summary("council", ranked, selected))
            store.write_manifest(manifest)
            return manifest
        finally:
            self._finish(store, final_status)
            store.cleanup_worktrees(keep=self.keep_worktrees)
            self._prune()

    def _council_implement(self, store: RunStore, providers: List[str],
                           prompt: str, profile: str) -> Dict[str, Any]:
        import threading
        candidates: List[Candidate] = []
        lock = threading.Lock()
        workdirs = {provider: store.create_worktree(provider)
                    for provider in providers}

        def run_one(provider: str) -> None:
            if run_ctrl.cancel_requested(store.run_dir):
                return
            adapter = self.adapter(provider)
            if not adapter.probe().available:
                return
            request = self._request(prompt, workdirs[provider], profile, provider)
            result = self._drive(adapter, request, store, provider)
            candidate = Candidate(role=provider, provider=provider, result=result)
            if result.status != "cancelled":
                candidate.gates = self.run_gates(workdirs[provider], store, provider)
                candidate.patch_path = store.export_patch(provider)
                candidate.diff_hash = store.diff_hash(provider)
            with lock:
                candidates.append(candidate)

        threads = [threading.Thread(target=run_one, args=(p,)) for p in providers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return {"_candidates": candidates}

    # -- helpers -----------------------------------------------------------------

    def _request(self, prompt: str, workdir, profile: str,
                 provider: str) -> AgentRequest:
        timeout = float(self.cfg.get("providers", provider, "timeout_seconds",
                                     default=1800))
        model = str(self.cfg.get("providers", provider, "model", default="") or "")
        # Outer Absoloop orchestration + inner native teams/subagents.
        tasked = with_delegation(prompt, provider, profile)
        return AgentRequest(prompt=tasked, cwd=str(workdir),
                            permission_profile=profile, model=model,
                            timeout_seconds=timeout)

    def _manifest(self, store: RunStore, strategy: str,
                  candidates: List[Candidate], probes, prompt: str,
                  profile: str, selected: Optional[str],
                  extra: Optional[Dict[str, Any]] = None,
                  status: str = "completed") -> Dict[str, Any]:
        import hashlib
        manifest: Dict[str, Any] = {
            "run_id": store.run_id,
            "strategy": strategy,
            "status": status,
            "live": False,
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            "permission_profile": profile,
            "selected_candidate": selected,
            "providers": [{
                "name": probe.info.name,
                "executable": probe.info.executable,
                "version": probe.info.version,
                "capabilities": probe.capabilities.to_json(),
            } for probe in probes],
            "candidates": [{
                "role": candidate.role,
                "provider": candidate.provider,
                "status": candidate.result.status if candidate.result else "not_run",
                "exit_code": candidate.result.exit_code if candidate.result else None,
                "session_id": (candidate.result.session.native_id
                               if candidate.result and candidate.result.session else None),
                "usage": candidate.result.usage if candidate.result else {},
                "cost_usd": candidate.result.cost_usd if candidate.result else None,
                "duration_seconds": (candidate.result.duration_seconds
                                     if candidate.result else 0),
                "gates": [{"name": gate.name, "passed": gate.passed,
                           "exit_code": gate.exit_code} for gate in candidate.gates],
                "diff_hash": candidate.diff_hash,
                "artifacts": {
                    "final": f"candidates/{candidate.role}/final.json",
                    "patch": f"candidates/{candidate.role}/diff.patch",
                    "test_log": f"candidates/{candidate.role}/test.log",
                    "stderr": f"candidates/{candidate.role}/stderr.log",
                },
            } for candidate in candidates],
        }
        manifest.update(extra or {})
        return manifest

    def _summary(self, strategy: str, ranked: List[Candidate],
                 selected: Optional[str]) -> str:
        lines = [f"# Absoloop run — strategy: {strategy}", ""]
        chosen = selected or "none (no candidate survived the gates)"
        lines.append(f"Selected candidate: **{chosen}**")
        lines.append("")
        for candidate in ranked:
            status = candidate.result.status if candidate.result else "not_run"
            gates = ", ".join(f"{g.name}:{'PASS' if g.passed else 'FAIL'}"
                              for g in candidate.gates) or "no gates configured"
            lines.append(f"- `{candidate.role}` — {status}; gates: {gates}; "
                         f"diff {candidate.diff_size} bytes")
        return "\n".join(lines) + "\n"

    def _prune(self) -> None:
        retention = int(self.cfg.get("artifacts", "retention_runs", default=20))
        prune_runs(self.root, retention)
