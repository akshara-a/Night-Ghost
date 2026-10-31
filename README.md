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
| **Fully Local** | Runs open-weight models via Ollama or llama.cpp. Subprocesses run with network egress blocked (`ALL_PROXY=http://127.0.0.1:0`). |
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
Triggered at your scheduled time (e.g. 01:00 AM) by cron or systemd:

```bash
nightghost run
```

### 🌅 Morning: Review Your Progress
When you start your day:

```bash
# Read the transparent morning summary
nightghost report

# Inspect the isolated branch diff
git diff main..nightghost/$(date +%Y-%m-%d)
```

- If satisfied: merge the branch or cherry-pick the commits.
- If not: discard cleanly with one command:
  ```bash
  nightghost discard
  ```

---

## 📋 CLI Reference

| Command | Description |
|---|---|
| `nightghost init` | Scaffold `nightghost.yaml` and `.nightghost/` directory. |
| `nightghost note [TEXT]` | View or set tonight's evening task note. |
| `nightghost run [--dry-run]` | Wake and execute the overnight pipeline (or simulate with `--dry-run`). |
| `nightghost report [DATE]` | Render the morning report in terminal (defaults to latest). |
| `nightghost status` | View current branch, task, baseline test status, and kill switch state. |
| `nightghost sleep` | Arm the overnight run. |
| `nightghost schedule` | Generate copy-paste crontab or systemd timer configurations. |
| `nightghost stop` | Emergency kill switch: creates `.nightghost/STOP` to halt execution. |
| `nightghost discard` | Delete the ghost branch and return cleanly to your base branch. |
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
