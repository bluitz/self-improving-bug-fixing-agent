#!/usr/bin/env python3
"""
bug_fix_loop.py

Implements the "Loop Over Bug Scenarios" for a TypeScript/React repository:
- Identify bug-fix commits
- For each, check out the buggy parent, run tests, and attempt to fix via an LLM (Claude)
- Iteratively refine fix attempts
- If failing, invoke a Prompt-Refiner that sees the human diff and outputs GENERALIZED guidelines (no code leakage)
- Persist guidelines and logs; measure metrics (pass rate, attempts, diff size)

Requirements:
  - Python 3.9+
  - git CLI installed
  - Node + your repo's test command available (configurable)
  - (Optional) anthropic Python package if you want real Claude calls: pip install anthropic

Usage:
  python bug_fix_loop.py --repo /path/to/repo --limit 100 \
    --test-cmd "npm test --silent -- --ci" --require-approval false

NOTE:
  - This script does not alter your git history (uses working tree changes).
  - It writes logs to ./agent_logs and guidelines to ./prompt_guidelines.md by default.

Author: you :)
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

# ----------------------------
# Configuration and defaults
# ----------------------------

@dataclass
class Config:
    repo_path: Path
    limit_commits: int = 100
    test_command: str = "npm test --silent -- --ci"
    node_env: Dict[str, str] = dataclasses.field(default_factory=lambda: {"CI": "1"})
    max_fix_attempts_per_bug: int = 3
    max_refine_iterations_per_bug: int = 10  # total "meta" refinements for a bug
    logs_dir: Path = Path("agent_logs")
    guidelines_file: Path = Path("prompt_guidelines.md")
    llm_model_bug_fixer: str = "claude-3-5-sonnet-20240620"
    llm_model_refiner: str = "claude-3-5-sonnet-20240620"
    require_approval: bool = False  # if True, ask before applying LLM patch
    max_context_bytes_per_file: int = 60_000  # guardrail to avoid huge prompts
    # Heuristic to detect bug-fix commits (can be customized):
    bugfix_grep: List[str] = dataclasses.field(
        default_factory=lambda: ["fix", "bug", "hotfix", "regression"]
    )

# ----------------------------
# Utilities
# ----------------------------

def run_cmd(cmd: Union[str, List[str]], cwd: Optional[Path]=None,
            env: Optional[Dict[str, str]]=None, check: bool=False, timeout: Optional[int]=None) -> Tuple[int, str, str]:
    """Run a shell command; return (code, stdout, stderr)."""
    if isinstance(cmd, str):
        to_run = cmd
        shell = True
    else:
        to_run = cmd
        shell = False
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    p = subprocess.Popen(
        to_run,
        cwd=str(cwd) if cwd else None,
        env=proc_env,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return (124, out, err)
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, out, err)
    return (p.returncode, out, err)

def ensure_clean_worktree(repo: Path) -> None:
    """Reset and clean working tree (DANGEROUS: discards uncommitted changes)."""
    run_cmd(["git", "reset", "--hard"], cwd=repo)
    run_cmd(["git", "clean", "-fd"], cwd=repo)

def git_checkout_detached(repo: Path, commit: str) -> None:
    """Checkout a commit in detached HEAD."""
    # Hard reset first to avoid conflicts
    ensure_clean_worktree(repo)
    code, _, err = run_cmd(["git", "checkout", "-f", commit], cwd=repo)
    if code != 0:
        raise RuntimeError(f"git checkout failed for {commit}: {err}")

def git_rev_parse_parent(repo: Path, commit: str) -> Optional[str]:
    code, out, _ = run_cmd(["git", "rev-parse", f"{commit}^"], cwd=repo)
    return out.strip() if code == 0 else None

def git_diff_name_only(repo: Path, a: str, b: str) -> List[str]:
    code, out, _ = run_cmd(["git", "diff", "--name-only", f"{a}..{b}"], cwd=repo)
    return [line.strip() for line in out.splitlines() if line.strip()]

def git_diff_unified(repo: Path, a: str, b: str) -> str:
    code, out, _ = run_cmd(["git", "diff", "--unified", f"{a}..{b}"], cwd=repo)
    return out

def detect_bug_fix_commits(cfg: Config) -> List[Tuple[str, str, str]]:
    """
    Return list of tuples: (buggy_parent_commit, fix_commit, subject)
    using simple grep over commit messages to find 'bug-fix like' commits.
    """
    patterns = []
    for term in cfg.bugfix_grep:
        patterns += ["--grep", term]
    code, out, err = run_cmd(
        ["git", "log", "--no-merges", "-n", str(cfg.limit_commits), "--pretty=format:%H|%s|%P"] + patterns,
        cwd=cfg.repo_path
    )
    if code != 0:
        raise RuntimeError(f"git log failed: {err}")
    pairs: List[Tuple[str, str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        commit, subject, parents = (line.split("|", 2) + [""])[:3]
        # first parent is the buggy base for a fix commit (heuristic)
        parent = parents.split(" ")[0] if parents else ""
        if parent:
            pairs.append((parent, commit, subject))
    return pairs

def read_file_bytes(path: Path, max_bytes: int) -> str:
    try:
        data = path.read_bytes()
        if len(data) > max_bytes:
            # Try to keep start+end slices for context
            head = data[: max_bytes // 2]
            tail = data[-max_bytes // 2 :]
            return head.decode("utf-8", errors="ignore") + "\n\n/* …snip… */\n\n" + tail.decode("utf-8", errors="ignore")
        return data.decode("utf-8", errors="ignore")
    except FileNotFoundError:
        return ""

def parse_jest_like_failures(output: str) -> Dict[str, Union[str, List[str]]]:
    """
    Heuristic parser for Jest-like output.
    Returns dict with keys: 'failed_files', 'first_failed_test', 'raw'
    """
    failed_files = []
    for m in re.finditer(r"^FAIL\s+([^\n\r]+)", output, flags=re.MULTILINE):
        failed_files.append(m.group(1).strip())

    # Try to extract first failed test name:
    first_failed_test = None
    m2 = re.search(r"●\s+([^\n\r]+)", output)
    if m2:
        first_failed_test = m2.group(1).strip()

    return {"failed_files": failed_files, "first_failed_test": first_failed_test, "raw": output}

def count_diff_lines(diff_text: str) -> Tuple[int, int]:
    """Return (added, removed) line counts from unified diff text."""
    added = sum(1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---"))
    return (added, removed)

def apply_unified_diff_with_git(repo: Path, diff_text: str) -> bool:
    """Try applying unified diff via `git apply`."""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".patch") as f:
        f.write(diff_text)
        patch_path = f.name
    try:
        code, out, err = run_cmd(["git", "apply", "--whitespace=fix", patch_path], cwd=repo)
        if code != 0:
            # Try with --reject to see partial applicability
            code2, out2, err2 = run_cmd(["git", "apply", "--reject", "--whitespace=fix", patch_path], cwd=repo)
            if code2 != 0:
                return False
        return True
    finally:
        try:
            os.remove(patch_path)
        except OSError:
            pass

# ----------------------------
# LLM client abstractions
# ----------------------------

class LLMClient:
    """Abstract interface for a chat-completion style LLM."""

    def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        raise NotImplementedError

class ClaudeClient(LLMClient):
    """
    Real Claude client via anthropic SDK if available.
    Set ANTHROPIC_API_KEY in your environment.
    """
    def __init__(self):
        try:
            import anthropic  # type: ignore
        except ImportError:
            self._anthropic = None
            return
        self._anthropic = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def complete(self, system_prompt: str, user_prompt: str, model: str) -> str:
        if self._anthropic is None:
            # Fallback placeholder (no external calls)
            return "/* ERROR: anthropic not installed or API key missing. */"
        msg = self._anthropic.messages.create(
            model=model,
            max_tokens=5000,
            temperature=0.2,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Return as plain text:
        chunks = []
        for block in msg.content:
            if block.type == "text":
                chunks.append(block.text)
        return "\n".join(chunks)

# ----------------------------
# Agents
# ----------------------------

@dataclass
class Guidelines:
    """Holds and persists generalized guidelines that evolve over bugs."""
    path: Path
    items: List[str] = field(default_factory=list)

    def load(self) -> None:
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8", errors="ignore")
            blocks = re.findall(r"^- (.+)$", text, flags=re.MULTILINE)
            self.items = [b.strip() for b in blocks if b.strip()]

    def save(self) -> None:
        header = "# Bug-Fixer Prompt Guidelines (evolving)\n\n" \
                 "These are generalized, non-specific lessons learned. Avoid revealing exact historical fixes.\n\n"
        bullet_list = "".join(f"- {it}\n" for it in self.items)
        self.path.write_text(header + bullet_list, encoding="utf-8")

    def as_bullets(self) -> str:
        if not self.items:
            return "- Think step-by-step. Isolate root cause before editing.\n" \
                   "- Do not modify tests. Change only the minimal app code.\n" \
                   "- Prefer small, targeted diffs. Preserve public APIs.\n" \
                   "- Check for null/undefined, off-by-one, and incorrect assumptions.\n" \
                   "- Respect TypeScript types; ensure code compiles.\n"
        return "".join(f"- {it}\n" for it in self.items)

@dataclass
class BugScenario:
    parent_commit: str
    fix_commit: str
    subject: str
    files_changed: List[str] = field(default_factory=list)
    human_diff: str = ""  # diff between parent..fix

class BugFixerAgent:
    """Generates a code patch (unified diff) to fix the failing test, without seeing tests' code modifications."""

    def __init__(self, llm: LLMClient, cfg: Config, guidelines: Guidelines):
        self.llm = llm
        self.cfg = cfg
        self.guidelines = guidelines

    def build_system_prompt(self) -> str:
        return textwrap.dedent(f"""
        You are an expert TypeScript/React engineer. Your task is to FIX A BUG so that the test suite passes.

        RULES:
        - Do NOT modify test files. Only edit application/library code.
        - Prefer the SMALLEST diff that resolves the failure.
        - Respect existing abstractions and TypeScript types; code must compile.
        - Keep behavior backward-compatible unless the failing test indicates otherwise.
        - Add defensive checks (null/undefined) where appropriate, but avoid overfitting.
        - Output a VALID unified diff (git-style). Nothing else.

        GENERALIZED GUIDELINES (learned so far):
        {self.guidelines.as_bullets()}
        """)

    def build_user_prompt(self, scenario: BugScenario, failing_info: Dict[str, Union[str, List[str]]],
                          code_context: Dict[str, str]) -> str:
        failed_files = failing_info.get("failed_files", []) or []
        first_test = failing_info.get("first_failed_test") or "Unknown test"
        failure_snippet = failing_info.get("raw") or ""

        files_block = []
        for relpath, content in code_context.items():
            # Wrap each file's content
            block = f"\n===== FILE: {relpath} =====\n{content}\n"
            files_block.append(block)

        files_text = "".join(files_block) if files_block else "(No focused files provided; reason about root cause)"

        changed_files_list = "\n".join(f"- {p}" for p in scenario.files_changed) or "(unknown)"

        return textwrap.dedent(f"""
        CONTEXT:
        - You are on commit {scenario.parent_commit} (bug present). The human later fixed it in {scenario.fix_commit}.
        - Test command failed. First failing test: {first_test}
        - Test runner output (excerpt):
          ```
          {failure_snippet[:4000]}
          ```
        - Files suspected relevant (from later human fix): 
          {changed_files_list}

        RELEVANT CODE (from buggy commit version):
        {files_text}

        TASK:
        - Identify the likely root cause and propose a minimal fix.
        - DO NOT change tests. Only change code.
        - Return a unified diff (patch) that applies cleanly at the current working tree root.
        - Keep changes small and targeted.
        """)

    def propose_patch(self, scenario: BugScenario, failing_info: Dict[str, Union[str, List[str]]],
                      code_context: Dict[str, str]) -> str:
        sys_prompt = self.build_system_prompt()
        user_prompt = self.build_user_prompt(scenario, failing_info, code_context)
        return self.llm.complete(sys_prompt, user_prompt, self.cfg.llm_model_bug_fixer)

class PromptRefinerAgent:
    """
    Reads the human diff and the agent's failed attempt(s), and suggests *generalized* prompt guidelines,
    without leaking exact code from the human diff.
    """

    def __init__(self, llm: LLMClient, cfg: Config):
        self.llm = llm
        self.cfg = cfg

    def refine(self, scenario: BugScenario, last_agent_diff: str, failing_info: Dict[str, Union[str, List[str]]],
               current_guidelines: str) -> List[str]:
        sys_prompt = textwrap.dedent("""
        You are a senior code-reviewer and meta-coach for a bug-fixing agent.
        You will be shown:
          - The agent's unsuccessful patch attempt (unified diff)
          - The human's ACTUAL historical diff (ground truth) that fixed the bug
          - A brief excerpt of failing test output
        Your job:
          - Derive 2-4 GENERALIZED guidelines that would help the agent catch this class of issue next time.
          - DO NOT reveal or paraphrase the exact human code; speak only in abstract, reusable heuristics.
          - Keep each guideline concise (max ~120 chars).
        Output JSON array of strings (guidelines). Nothing else.
        """)
        user_prompt = textwrap.dedent(f"""
        Failing test excerpt:
        ```
        {str(failing_info.get("raw",""))[:2000]}
        ```

        Agent's (failed) diff attempt:
        ```
        {last_agent_diff[:4000]}
        ```

        Human FIX (historical unified diff) for this bug (DO NOT leak specifics; use only to infer abstract lessons):
        ```
        {scenario.human_diff[:6000]}
        ```

        Current generalized guidelines:
        {current_guidelines}

        Return STRICT JSON array with NEW or REVISED high-level guidelines. Avoid duplicates.
        """)
        out = self.llm.complete(sys_prompt, user_prompt, self.cfg.llm_model_refiner).strip()
        # Attempt to parse JSON array from output robustly
        try:
            arr = json.loads(out)
            if isinstance(arr, list):
                cleaned = []
                for it in arr:
                    if not isinstance(it, str): continue
                    s = it.strip()
                    if s and s not in cleaned:
                        cleaned.append(s)
                return cleaned[:4]
        except Exception:
            pass
        # Fallback: extract lines starting with dash
        guidelines = []
        for line in out.splitlines():
            m = re.match(r'^\s*[-*]\s+(.*)$', line)
            if m:
                g = m.group(1).strip()
                if g and g not in guidelines:
                    guidelines.append(g)
        return guidelines[:4]

# ----------------------------
# Orchestration
# ----------------------------

class Orchestrator:
    def __init__(self, cfg: Config, llm_bug: LLMClient, llm_ref: LLMClient):
        self.cfg = cfg
        self.llm_bug = llm_bug
        self.llm_ref = llm_ref
        self.guidelines = Guidelines(cfg.guidelines_file)
        self.guidelines.load()
        self.bug_agent = BugFixerAgent(llm_bug, cfg, self.guidelines)
        self.refiner = PromptRefinerAgent(llm_ref, cfg)
        self.logs_dir = cfg.logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def approval(self, scenario: BugScenario, diff: str) -> bool:
        if not self.cfg.require_approval:
            return True
        print(f"\n[APPROVAL REQUIRED] Proposed diff for {scenario.parent_commit[:8]}..{scenario.fix_commit[:8]}:\n")
        print(diff)
        answer = input("\nApply this patch? [y/N]: ").strip().lower()
        return answer == "y"

    def run_tests(self) -> Tuple[bool, str]:
        code, out, err = run_cmd(self.cfg.test_command, cwd=self.cfg.repo_path, env=self.cfg.node_env)
        output = out + ("\n" + err if err else "")
        # Heuristic: exit code 0 => pass
        return (code == 0, output)

    def load_code_context(self, scenario: BugScenario) -> Dict[str, str]:
        """Load file contents for files changed in the human fix (from buggy parent state)."""
        ctx: Dict[str, str] = {}
        for rel in scenario.files_changed:
            p = (self.cfg.repo_path / rel)
            if not p.exists():
                # sometimes paths move; skip silently
                continue
            ctx[rel] = read_file_bytes(p, self.cfg.max_context_bytes_per_file)
        return ctx

    def write_log(self, scenario: BugScenario, content: str) -> None:
        name = f"{scenario.parent_commit[:10]}_{scenario.fix_commit[:10]}.md"
        (self.logs_dir / name).write_text(content, encoding="utf-8")

    def scenario_log_header(self, scenario: BugScenario) -> str:
        return textwrap.dedent(f"""\
        # Bug Scenario: {scenario.subject}
        - Parent (buggy): `{scenario.parent_commit}`
        - Fix commit (human): `{scenario.fix_commit}`
        - Files changed by human: {", ".join(scenario.files_changed) if scenario.files_changed else "(unknown)"}
        ---
        """)

    def loop_over_bug_scenarios(self, scenarios: List[BugScenario]) -> None:
        total = len(scenarios)
        print(f"[INFO] Processing {total} bug scenarios...\n")
        for idx, scenario in enumerate(scenarios, start=1):
            print(f"[INFO] ({idx}/{total}) {scenario.parent_commit[:8]}..{scenario.fix_commit[:8]}  {scenario.subject}")
            # checkout buggy parent
            try:
                git_checkout_detached(self.cfg.repo_path, scenario.parent_commit)
            except Exception as e:
                print(f"[WARN] Checkout failed; skipping scenario: {e}")
                continue

            # Baseline test: ensure there is a failing test at buggy parent
            passed, out = self.run_tests()
            if passed:
                print("  - Tests already pass at parent. Skipping (not a failing baseline).")
                continue

            failing_info = parse_jest_like_failures(out)
            code_ctx = self.load_code_context(scenario)

            log_buf = [self.scenario_log_header(scenario)]
            log_buf.append("## Baseline failing test output\n\n```\n" + failing_info.get("raw","")[:8000] + "\n```\n")

            success = False
            attempts = 0
            last_agent_diff = ""

            # Inner fix attempts
            for attempt in range(1, self.cfg.max_fix_attempts_per_bug + 1):
                attempts = attempt
                print(f"  - Attempt {attempt}: proposing patch...")
                proposed = self.bug_agent.propose_patch(scenario, failing_info, code_ctx).strip()

                # Basic sanity: ensure it looks like a unified diff
                looks_like_diff = re.search(r"^\s*diff\s+--git\s", proposed, flags=re.MULTILINE) or \
                                  re.search(r"^\s*---\s", proposed, flags=re.MULTILINE)
                if not looks_like_diff:
                    print("    ! LLM did not return a recognizable unified diff. Will refine later if needed.")
                    last_agent_diff = proposed
                else:
                    last_agent_diff = proposed
                    if not self.approval(scenario, proposed):
                        print("    ! User declined patch; marking attempt failed.")
                    else:
                        # Try to apply
                        ensure_clean_worktree(self.cfg.repo_path)
                        ok = apply_unified_diff_with_git(self.cfg.repo_path, proposed)
                        if not ok:
                            print("    ! Failed to apply diff cleanly. Will refine later if needed.")
                        else:
                            # Re-run tests
                            print("    - Patch applied. Running tests...")
                            passed2, out2 = self.run_tests()
                            log_buf.append(f"### Attempt {attempt} proposed patch\n\n```\n{proposed[:18000]}\n```\n")
                            log_buf.append("### Attempt {0} test output\n\n```\n{1}\n```\n".format(attempt, out2[:8000]))
                            if passed2:
                                success = True
                                add, rm = count_diff_lines(proposed)
                                log_buf.append(f"**SUCCESS** on attempt {attempt}. Diff size: +{add}/-{rm}\n")
                                break
                            else:
                                failing_info = parse_jest_like_failures(out2)
                                print("    ! Tests still failing after patch.")

            if success:
                self.write_log(scenario, "\n".join(log_buf))
                continue

            # Prompt-refinement loop using human diff (no leakage)
            refine_success = False
            for refine_round in range(1, self.cfg.max_refine_iterations_per_bug + 1):
                print(f"  - Refinement {refine_round}: deriving generalized guidelines from human diff...")
                new_guides = self.refiner.refine(scenario, last_agent_diff, failing_info, self.guidelines.as_bullets())
                if not new_guides:
                    log_buf.append(f"### Refinement {refine_round}\nNo usable guidelines returned.\n")
                    print("    ! No guidelines returned.")
                else:
                    # Merge new guidelines (dedupe)
                    merged = list(self.guidelines.items)
                    for g in new_guides:
                        if g not in merged:
                            merged.append(g)
                    self.guidelines.items = merged
                    self.guidelines.save()
                    log_buf.append(f"### Refinement {refine_round} new guidelines\n" +
                                   "".join(f"- {g}\n" for g in new_guides) + "\n")

                # Reset to buggy parent and try again with updated prompt
                try:
                    git_checkout_detached(self.cfg.repo_path, scenario.parent_commit)
                except Exception as e:
                    print(f"    ! Checkout failed during refinement: {e}")
                    break

                passed, out = self.run_tests()
                if passed:
                    # Strange: baseline now passes; log and continue
                    log_buf.append("Baseline unexpectedly passes after reset; skipping attempts.\n")
                    break

                failing_info = parse_jest_like_failures(out)
                code_ctx = self.load_code_context(scenario)
                # Try attempts again with new guidelines
                last_agent_diff = ""
                success = False
                for attempt in range(1, self.cfg.max_fix_attempts_per_bug + 1):
                    print(f"    - Attempt {attempt} (post-refine): proposing patch...")
                    proposed = self.bug_agent.propose_patch(scenario, failing_info, code_ctx).strip()
                    looks_like_diff = re.search(r"^\s*diff\s+--git\s", proposed, flags=re.MULTILINE) or \
                                      re.search(r"^\s*---\s", proposed, flags=re.MULTILINE)
                    if not looks_like_diff:
                        last_agent_diff = proposed
                        print("      ! Not a unified diff. Continuing.")
                        continue
                    last_agent_diff = proposed
                    if not self.approval(scenario, proposed):
                        print("      ! User declined patch.")
                        continue
                    ensure_clean_worktree(self.cfg.repo_path)
                    ok = apply_unified_diff_with_git(self.cfg.repo_path, proposed)
                    if not ok:
                        print("      ! Diff application failed.")
                        continue
                    print("      - Patch applied. Running tests...")
                    passed2, out2 = self.run_tests()
                    log_buf.append(f"#### Post-refine attempt {attempt} proposed patch\n\n```\n{proposed[:18000]}\n```\n")
                    log_buf.append("#### Post-refine attempt {0} test output\n\n```\n{1}\n```\n".format(attempt, out2[:8000]))
                    if passed2:
                        success = True
                        add, rm = count_diff_lines(proposed)
                        log_buf.append(f"**SUCCESS after refinement {refine_round} attempt {attempt}**. Diff size: +{add}/-{rm}\n")
                        break
                if success:
                    refine_success = True
                    break

            if not (success or refine_success):
                log_buf.append("**FAILED** to fix after attempts and refinements.\n")

            self.write_log(scenario, "\n".join(log_buf))

# ----------------------------
# Wiring and main
# ----------------------------

def build_scenarios(cfg: Config) -> List[BugScenario]:
    pairs = detect_bug_fix_commits(cfg)
    scenarios: List[BugScenario] = []
    for parent, fix, subject in pairs:
        files = git_diff_name_only(cfg.repo_path, parent, fix)
        human_diff = git_diff_unified(cfg.repo_path, parent, fix)
        scenarios.append(BugScenario(parent_commit=parent, fix_commit=fix, subject=subject,
                                     files_changed=files, human_diff=human_diff))
    return scenarios

def parse_args() -> Config:
    ap = argparse.ArgumentParser(description="Loop over bug scenarios and attempt autonomous fixes.")
    ap.add_argument("--repo", required=True, help="Path to git repo")
    ap.add_argument("--limit", type=int, default=100, help="Max bug-fix commits to consider")
    ap.add_argument("--test-cmd", default="npm test --silent -- --ci", help="Command to run tests")
    ap.add_argument("--require-approval", default="false", choices=["true","false"], help="Ask before applying patches")
    ap.add_argument("--guidelines-file", default="prompt_guidelines.md", help="Path to guidelines .md")
    ap.add_argument("--logs-dir", default="agent_logs", help="Directory to write scenario logs")
    ap.add_argument("--max-attempts", type=int, default=3, help="Fix attempts per baseline/refine round")
    ap.add_argument("--max-refines", type=int, default=10, help="Max prompt-refine iterations per bug")
    ap.add_argument("--bugfix-terms", default="fix,bug,hotfix,regression", help="Comma-separated grep terms")
    ap.add_argument("--model-fixer", default="claude-3-5-sonnet-20240620", help="Claude model for Bug-Fixer")
    ap.add_argument("--model-refiner", default="claude-3-5-sonnet-20240620", help="Claude model for Refiner")
    args = ap.parse_args()

    cfg = Config(
        repo_path=Path(args.repo).resolve(),
        limit_commits=args.limit,
        test_command=args.test_cmd,
        require_approval=(args.require_approval.lower()=="true"),
        guidelines_file=Path(args.guidelines_file),
        logs_dir=Path(args.logs_dir),
        max_fix_attempts_per_bug=args.max_attempts,
        max_refine_iterations_per_bug=args.max_refines,
        bugfix_grep=[t.strip() for t in args.bugfix_terms.split(",") if t.strip()],
        llm_model_bug_fixer=args.model_fixer,
        llm_model_refiner=args.model_refiner,
    )
    return cfg

def main():
    cfg = parse_args()
    if not (cfg.repo_path / ".git").exists():
        print(f"[ERROR] Not a git repo: {cfg.repo_path}")
        sys.exit(2)

    # Initialize LLM clients
    llm_bug = ClaudeClient()
    llm_ref = llm_bug  # same client; separate roles/prompts

    # Build scenarios
    scenarios = build_scenarios(cfg)
    if not scenarios:
        print("[INFO] No bug-fix commits detected with current grep filters.")
        sys.exit(0)

    orch = Orchestrator(cfg, llm_bug, llm_ref)
    orch.loop_over_bug_scenarios(scenarios)
    print("\n[DONE] Logs written to:", cfg.logs_dir.resolve())
    print("[DONE] Guidelines at:", cfg.guidelines_file.resolve())

if __name__ == "__main__":
    main()
