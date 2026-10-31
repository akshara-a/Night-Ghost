# Night Ghost Safety Model

> Autonomous overnight coding requires paranoid, defense-in-depth safety guarantees. Night Ghost is designed from the ground up so that you never wake up to broken code, deleted databases, leaked secrets, or mangled tests.

---

## 1. Core Principles (Non-Negotiable)

| Principle | How It Is Enforced |
|---|---|
| **Fully Local** | Model runs entirely on your machine via Ollama or llama.cpp. Subprocesses run with network egress blocked (`ALL_PROXY=http://127.0.0.1:0`). |
| **Only Touches Allowed Files** | Explicit `allow` / `deny` globs in `nightghost.yaml`. All path checks happen in Python code, resolving real symlinks and directory traversal, never relying on LLM obedience. |
| **Small, Safe, Well-Tested Changes** | Diff size caps, file count caps, runtime limits, and test-gated commits. Any test regression immediately resets the working directory. |
| **Transparent Morning Report** | Every single action is written to `actions.jsonl`. The morning markdown report is generated strictly from the structured log, not from LLM hallucination. |

---

## 2. Guardrail Enforcement Hierarchy

Every file read, file write, shell command, and code edit passes through the `Guardrail` engine before execution:

```
[Agent Tool Request]
       │
       ▼
1. Kill Switch Check
   - Is .nightghost/STOP present? ──▶ HALT IMMEDIATELY
       │
       ▼
2. Path Traversal & Normalization Check
   - Resolve real path against repo root.
   - Any path with '..' escaping repo root or symlinks to outside? ──▶ REJECT
       │
       ▼
3. Denylist Verification (Precedence)
   - Matches .env*, migrations/**, *.pem, *.key, .github/**, *.lock, .git/**? ──▶ REJECT
       │
       ▼
4. Content Secrets Inspection
   - Contains RSA/EC private keys, AWS keys, GitHub tokens, JWTs? ──▶ REJECT
       │
       ▼
5. Assertion Integrity Check
   - Modifies or deletes any existing assertion in an existing test file? ──▶ REJECT
       │
       ▼
6. Budget Check
   - Total files changed > max_files_changed? ──▶ REJECT
   - Total lines changed > max_lines_changed? ──▶ REJECT
       │
       ▼
7. Allowlist Check
   - Matches scope.allow (e.g. src/**, tests/**)? ──▶ EXECUTE WRITE
```

---

## 3. Test-Gated Commits & Regression Rollback

Night Ghost commits changes **only** when tests pass without regression:

1. **Preflight Baseline**: Before starting, the test suite is run and pass/fail counts are recorded.
2. **Step Verification**: When an individual step's edits are applied, the test suite is run again.
3. **Regression Detection**:
   - New test failures? `current.failed > baseline.failed` ──▶ **FAIL**
   - New test errors? `current.errors > baseline.errors` ──▶ **FAIL**
   - Passing test drop? `current.passed < baseline.passed` ──▶ **FAIL**
4. **Retry Loop**: Up to `max_retries_per_step` attempts.
5. **Atomic Revert**: If retries are exhausted without passing tests, the working tree is cleanly reset (`git reset` + checkout), and the step is logged as reverted in the morning report.

---

## 4. Assertion Tampering Protection

A common pitfall with automated coding agents is weakening existing tests (e.g., changing `assert result == 42` to `assert True`) to make buggy code pass.

Night Ghost detects this at the AST / regex level:
- If a target file is an existing test file, all assertion statements are extracted from the baseline.
- If any existing assertion line is deleted or altered in the proposed new content, the write is **instantly blocked** with `EXISTING_ASSERTION_MODIFIED`.
- Adding **new** test assertions or new test functions is always permitted.

---

## 5. Emergency Stop (Kill Switch)

If you need to halt Night Ghost immediately during an overnight run:
```bash
nightghost stop
```
Or simply create a file named `.nightghost/STOP` in the repository root. The runner detects this at step boundaries and terminates safely without corrupting Git state.
