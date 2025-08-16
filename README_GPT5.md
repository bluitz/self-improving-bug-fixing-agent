## Self-Improving Bug-Fixing Agent (JavaScript)

A lightweight, extensible agent that learns to fix bugs by iterating through a repository’s historical bug-fix commits. It “time-travels” to the buggy parent commit, runs tests, proposes a fix, re-tests, and, if needed, refines its own prompts using the human ground-truth diff for guidance (without revealing the exact solution to the fixer role).

### Key Goals

- **Autonomy first**: run end-to-end with minimal human input; optional UI for oversight.
- **Extensibility**: swappable LLM providers, test runners, repositories, and patch strategies via small interfaces.
- **Maintainability**: modular JS (ESM), clear boundaries, strong logging, and deterministic orchestration.
- **Data capture**: persist attempts, diffs, metrics, and prompt evolution for evaluation and future training.

### Core Architecture (Ports & Adapters)

- **Orchestrator (core)**: deterministic state machine driving the workflow.
- **BugFixerAgent (port)**: proposes code fixes from failing tests + code context.
- **CriticAgent (port)**: reads human fix diff and agent attempts to emit abstract guidance (prompt evolution), never concrete code.
- **Tester (port)**: runs the test suite and returns structured failures.
- **VCS (port)**: git operations to checkout parent/fix commits and read diffs.
- **PatchApplier (port)**: applies agent proposals safely; unified-diff preferred, full-file fallback.
- **Artifacts & Telemetry**: structured logs, run manifests, attempt transcripts, metrics.

Adapters implement the ports for concrete tools (e.g., Anthropic for LLM, Jest for tests, Git via simple-git). This isolates the core logic and keeps the system easy to evolve.

### Suggested Project Structure

```
/agent
  /core                  # orchestrator state machine, run loop
  /ports                 # Type-like JS interfaces (via JSDoc) for LLM, Tester, VCS, PatchApplier
  /adapters
    /llm                 # anthropic.js, openai.js (optional)
    /tester              # jest.js, mocha.js (optional)
    /vcs                 # git.js (simple-git)
    /patch               # unifiedDiff.js, fullFileReplace.js
  /prompt
    base.md              # initial system prompt (fixer)
    critic.md            # critic prompt template
    guidelines.json      # accumulated abstract rules
  /util                  # logging, fs helpers, parsing, diff utils
  /schemas               # JSON schemas for run logs & metrics (optional)
/config
  agent.config.json      # repo path, test cmd, LLM provider, limits
  providers.json         # API keys, model names, rate limits
/data
  /runs/<timestamp>/     # artifacts per run (logs, attempts, diffs, reports)
/docs                    # additional docs
```

### Dependencies (minimal, JS/ESM)

- Git: `simple-git`
- Processes: `execa`
- Diff parsing/apply: `diff`, `diff3`, or a tiny custom applier (plus full-file fallback)
- Logging: `pino` (or `winston`)
- Config: `zod` (runtime validation) or lightweight manual validate
- Optional UI later: React + Vite (separate package)

### Configuration

- `config/agent.config.json`
  - `repoPath`: absolute path to target repo
  - `testCommand`: e.g., `npm test --silent` or `pnpm test`
  - `searchBugFixCommits`: strategy: `messageContains:["fix","bug"]` (overrideable)
  - `maxAttemptsPerBug`: 3
  - `maxRefineIterations`: 10
  - `llmProvider`: `anthropic` (default)
- `config/providers.json`
  - `ANTHROPIC_API_KEY`, `model` (e.g., `claude-3-5-sonnet-latest`)

Environment variables can override provider keys/models at runtime.

### End-to-End Flow

1. Find bug-fix commits, pair each fix with its parent (buggy) commit.
2. For each pair: checkout parent; run tests; capture failures.
3. Prepare context (failing test excerpts, suspected files, and code snippets).
4. BugFixerAgent returns a proposed patch (unified diff or full file replacement).
5. Apply patch; re-run tests; record result.
6. If unresolved after N tries, CriticAgent reads the human diff and agent attempts, emits abstract guidance (no code). Append to `prompt/guidelines.json`, reset code, retry.
7. On success, log artifacts (final diff, attempts, guidance) and move to next bug.

### Implementation Plan (many small, shippable steps)

1. Bootstrap repo (Node 20+, ESM, lint, format, pino logging). Add `config/` and `/agent` skeletons.
2. Implement VCS adapter (`git.js`): checkout by hash, read commit pairs, get diff for a commit.
3. Implement Tester adapter (`jest.js`): run command, parse failures into `{testsFailed, failures:[{name,file,line,msg,stack}]}`.
4. Implement PatchApplier: start with full-file replace; add unified-diff support; validate compile step.
5. Define core data contracts in JS via JSDoc typedefs (in `/agent/ports`) and optional JSON Schemas for logs.
6. Implement BugFixerAgent adapter for Anthropic with a small wrapper (`anthropic.js`) and a provider-agnostic `LLMClient` port.
7. Create initial prompts (`/agent/prompt/base.md`, `/agent/prompt/critic.md`) and minimal `guidelines.json`.
8. Build Orchestrator: simple state machine with states: Discover -> SelectCommit -> Checkout -> Test -> FixAttempt -> Apply -> ReTest -> Success|Retry -> Critic -> PromptUpdate -> Reset -> Retry.
9. Add artifact manager (write attempt transcripts, diffs, prompts, test outputs) under `/data/runs/…`.
10. Add safety rails: forbid test file edits; patch scope guard (limit files to target repo); size & file-count limits.
11. Add commit-pair discovery strategies (by message, by labels/tags, or provided list); make them pluggable.
12. Add targeted context builder: heuristics to include likely files (e.g., files changed in the human fix commit) without showing the fix content.
13. Introduce CriticAgent: compare human diff vs attempts, emit abstract guidelines; append to `guidelines.json`.
14. Add metrics: success rate, attempts per bug, diff size, time & tokens; write a per-run summary.
15. Add a thin CLI (`node ./agent/cli.js run --config config/agent.config.json`) with subcommands: `discover`, `run`, `resume`, `report`.
16. Optional UI (separate package): read `/data/runs` and stream live state via simple WebSocket server.
17. CI integration: dry-run on a tiny sample; artifact upload; budget guards.
18. Hardening: retry policies, provider backoff, idempotent resume, integrity checks for patches.

Each step is independently testable and mergeable.

### Extensibility Points

- **LLM providers**: implement `LLMClient` with `generateFix(context)`, `generateCritique(context)`.
- **Test runners**: implement `Tester` with `run()` and `parse()`; e.g., Mocha, Vitest.
- **Patch strategies**: stack new strategies (diff apply, AST transforms, full-file) with ordered fallbacks.
- **Discovery strategies**: add new commit-pair providers (e.g., GitHub API labels).
- **Prompt policies**: switch prompt renderers; maintain `guidelines.json` as a first-class artifact.

### Maintainability Guidelines

- JS/ESM only; use JSDoc typedefs for strong editor support without TypeScript.
- Small modules, pure functions; avoid side effects in core logic.
- Guard clauses, early returns, and strict error handling (never swallow errors).
- Structured logs with run/commit/attempt IDs; no ad-hoc `console.log` in core.
- Config validated at startup; fail fast with clear diagnostics.

### Safety & Guardrails

- Never edit test files; enforce denylist on patches.
- Limit patch size and touched files; require compile to succeed before retest.
- Sanitize file paths; operate only within configured repo root.
- Record every applied change; support automatic rollback to pristine checkout.

### Getting Started

1. Prereqs: Node 20+, Git, a target repo with tests and known bug-fix commits.
2. Copy this project next to your target repo (or point `repoPath` to it).
3. Set provider key: `export ANTHROPIC_API_KEY=...`.
4. Configure `config/agent.config.json` and `config/providers.json`.
5. Run: `node ./agent/cli.js discover` then `node ./agent/cli.js run`.

Note: CLI, adapters, and orchestrator are implemented incrementally per the plan above.

### Evaluation & Reporting

- Success rate across commit pairs, attempts per bug, diff size vs human, runtime, tokens.
- Per-run HTML/Markdown report generated from `/data/runs/*/summary.json`.

### Roadmap (Short)

- v0.1: CLI + git + jest + anthropic + full-file patch + artifacts.
- v0.2: Unified diff, targeted context, critic-driven prompt evolution.
- v0.3: UI dashboard, multiple repos, additional test runners/providers.

This README is intentionally concise; see `Plan for an Autonomous Bug-Fixing Agent.md` for background theory and rationale.
