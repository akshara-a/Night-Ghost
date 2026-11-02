# Night Ghost 👻

> **"I stopped halfway" → "I woke up further ahead."**
>
> A local, open-source agent that continues your unfinished coding work overnight on an isolated branch and leaves a transparent morning report.

---

## 🌙 The Problem

Developers and indie hackers end the day with half-finished code. By morning, context and momentum are gone, and the first part of the day is wasted re-orienting: *"Where was I? What was broken? What was I about to test?"*

## ☀️ The Solution

**Night Ghost** is a local CLI agent designed to bridge the evening-to-morning gap:

1. **Evening**: You drop a quick task note (`nightghost note "Add payment edge cases and clean up the helpers"`).
2. **Night**: While you sleep, Night Ghost wakes up, inspects where you left off, writes tests, does safe cleanups, runs your test suite, and commits verified work on an isolated branch (`nightghost/YYYY-MM-DD`).
3. **Morning**: You review a transparent diff and a factual markdown report generated directly from measured actions—not model memory.

---

## 🛡️ Core Principles (Non-Negotiable)

| Principle | How It Is Enforced |
|---|---|
| **Fully Local** | Runs open-weight models via Ollama or llama.cpp. Test subprocesses run with best-effort network blocking: poisoned proxy variables plus a socket shim that refuses non-loopback connections and DNS lookups in Python processes. (Not an OS sandbox; non-Python tools are only covered by the proxy variables.) |
| **Only Touches Allowed Files** | Explicit `allow` / `deny` globs in `nightghost.yaml`. All path checks happen in Python code, resolving symlinks and directory traversal. |
| **Small, Safe, Well-Tested Changes** | Diff size caps, line budgets, and test-gated commits. Any test regression immediately rolls back that step. |
| **Assertion Protection** | Hard rule: detecting edits to existing test assertions blocks the write immediately to prevent models from "fixing" tests by weakening them. |
| **Transparent Morning Report** | Every action is logged to `actions.jsonl`; `REPORT.md` is compiled from the event log, ensuring measured results. |

---

## 📐 Architecture

```
┌────────────┐   ┌───────────┐   ┌──────────────┐   ┌─────────────┐
│ Task note  │──▶│  Planner  │──▶│  Executor    │──▶│  Reporter   │
│ + config   │   │ (Local LLM)│  │ (LLM + tools)│   │ (log→MD)    │
└────────────┘   └───────────┘   └──────┬───────┘   └─────────────┘
                                        │
                  ┌─────────────────────┼─────────────────────┐
                  ▼                     ▼                     ▼
            ┌──────────┐         ┌────────────┐        ┌────────────┐
            │ Guardrail│         │ Test Runner│        │ Git Manager│
            │ (paths,  │         │ (sandboxed │        │ (branch,   │
            │  limits) │         │  subproc)  │        │  commits)  │
            └──────────┘         └────────────┘        └────────────┘
                                        │
                                  ┌─────▼──────┐
                                  │ Local LLM  │
                                  │ (Ollama)   │
                                  └────────────┘
```

---

## ⚡ Quickstart

### 1. Installation

Install via `pipx` or standard `pip`:

```bash
# Recommended via pipx
pipx install nightghost

# Or via pip
pip install nightghost
```

### 2. Initialize Your Project

In your repository root:

```bash
nightghost init
```

This creates `nightghost.yaml` and `.nightghost/task.md`.

### 3. Ensure Local Model Is Running

Make sure Ollama has a coder model ready (default: `qwen2.5-coder:14b`):

```bash
ollama run qwen2.5-coder:14b "echo test"
```

---

## 🔄 Daily Workflow

### 🌇 Evening: Drop Your Note
Before closing your laptop:

```bash
nightghost note "Add unit tests for charge() edge cases and format payment.py"
nightghost sleep
```

### 🌃 Night: Automatic Execution
Install the nightly schedule once (Task Scheduler on Windows, launchd on macOS, cron on Linux) and arm the repo:

```bash
nightghost schedule --install   # or: nightghost sleep --install
nightghost doctor               # verify git, config, test command and model first
```

Scheduled runs (`nightghost run --scheduled`) only execute while the repo is armed. Disarm with `nightghost sleep --cancel`.

### 🌅 Morning: Review Your Progress
```bash
nightghost report               # the transparent morning summary
nightghost diff                 # what changed (add --stat for a summary)
nightghost accept               # merge the ghost branch into your base branch
nightghost discard              # ...or throw it all away
```

Your original branch is always restored after a run, and any uncommitted work is snapshotted and re-applied.

---

## 📋 CLI Reference

Global options: `-C/--repo PATH`, `--config PATH`, `-v/--verbose`, `--version`. Shell completion: `nightghost --install-completion`.

| Command | Description |
|---|---|
| `nightghost init [--yes] [--model M]` | Scaffold `nightghost.yaml` (auto-detects the test command) and `.nightghost/`; offers to add it to `.gitignore`. |
| `nightghost doctor` | Check git, config, test command, model and task note, with fix hints. Exits 1 on failure. |
| `nightghost note [TEXT]` | View or set the task note. `note -` reads stdin; `note -e` opens `$EDITOR`. |
| `nightghost run [--dry-run] [--json]` | Run the pipeline with live progress. Exit codes: `0` success, `1` error, `2` partial (steps reverted/skipped). |
| `nightghost log [-n N] [-f]` | Show or follow the structured action log. |
| `nightghost report [DATE] [--list]` | Render a morning report (defaults to latest). |
| `nightghost diff [BRANCH] [--stat]` | Diff of the ghost branch against its base. |
| `nightghost accept [BRANCH] [--delete]` | Merge the ghost branch into its base with a merge commit. |
| `nightghost discard [BRANCH]` | Delete the ghost branch and return to your base branch. |
| `nightghost status [--json]` | Branch, armed state, model health, kill switch, ghost branches, last run. |
| `nightghost sleep [--install\|--cancel]` | Arm (or disarm) scheduled runs. |
| `nightghost schedule [--install\|--uninstall] [-p PLATFORM]` | Show or install the nightly schedule. |
| `nightghost stop` / `nightghost resume` | Engage / clear the `.nightghost/STOP` kill switch. |
| `nightghost version` | Display current installed version. |

---

## ⚙️ Configuration (`nightghost.yaml`)

```yaml
model:
  provider: ollama            # Local runner: ollama, llamacpp, or mock
  name: qwen2.5-coder:14b     # Recommended coder model
  temperature: 0.2
  context_window: 16384
  api_base: http://localhost:11434

task:
  file: .nightghost/task.md

scope:
  allow:
    - "src/**"
    - "tests/**"
  deny:
    - ".env*"
    - "migrations/**"
    - "**/*.pem"
    - "**/*.key"
    - ".github/**"
    - "*.lock"
    - ".git/**"
    - "nightghost.yaml"

commands:
  test: "pytest -q"
  lint: "ruff check ."
  format: "ruff format ."
  timeout_seconds: 300

limits:
  max_steps: 8
  max_files_changed: 15
  max_lines_changed: 800
  max_runtime_minutes: 120
  max_retries_per_step: 2

safety:
  network: "off"
  never_edit_existing_assertions: true
  require_green_baseline: false

schedule:
  at: "01:00"
```

---

## 📊 Sample Morning Report

Here is an authentic report generated by Night Ghost:

```markdown
# Night Ghost Report: 2026-11-01
Task: "Add tests for the payment module and clean the messy parts"
Branch: nightghost/2026-11-01 (7 commits, 9 files, +412 / −96)
Model: qwen2.5-coder:14b (local) · Runtime: 1h 42m

## Result at a glance
✅ Tests: 48 → 71 passing (0 failing) · Coverage payments/: 41% → 83%

## What I did
1. Added tests for `charge()` and `refund()` edge cases: commit a1b2c3
2. Extracted duplicated currency rounding into `round_money()`: commit d4e5f6
3. Added boundary tests for zero/negative values: commit 7f8e9a

## Needs your judgment
- `apply_discount()` returns different rounding for negative amounts. Looks like a bug; I did NOT change it. Test marked `xfail` with explanation.
- Two functions in `legacy_gateway.py` look dead; deletion left to you.

## Skipped / failed
- Step 5 (split `process_order`): reverted, 3 tests regressed after 2 attempts.

## Blocked by guardrails
- Attempted write to `migrations/0042.sql`: denied (denylist).

## How to review
```bash
git diff main..nightghost/2026-11-01
nightghost discard   # to throw it all away
```
```

---

## 🧪 Testing

Run the full test suite with:

```bash
pytest
```

Includes 33 unit and integration tests covering:
- Path traversal and symlink escapes
- Denylist and secret pattern interception
- Assertion tampering detection
- Budget limits and command allowlists
- Git branch isolation, step commits, and WIP snapshotting
- Output parsing for pytest and Vitest/Jest
- End-to-end multi-step overnight pipeline execution

---

## 🤝 Contributing

Contributions are welcome! Please check out [docs/safety.md](docs/safety.md).

## 📄 License

Night Ghost is released under the [MIT License](LICENSE).
