# Native macOS implementation plan

Status: implementation in progress; agent execution remains Linux x64/glibc only.
This is a development plan, not a macOS quick start.

## Goal and acceptance criteria

Run the existing setup, authentication, setup script, OpenCode attempt, scoring,
resume and publication workflows directly on macOS ARM64 and x86_64, without a
Linux VM. Keep the Linux backend and existing frozen baskets readable.

A macOS run must retain before-only source access, a private HOME, a clean Python
3.12 environment per attempt, pinned runtime provenance and durable evidence.
Unavailable isolation must stop execution before model access; there is no
unsandboxed fallback. Platform differences must be recorded in reports rather
than claiming that scores from different toolchains are identical environments.

## Implementation stages

### 1. Portable OpenCode provisioning (first MR)

- [x] Select the official Darwin ARM64 ZIP and Darwin x64 baseline ZIP for the
  exact configured release; keep Linux x64/glibc asset and cache keys unchanged.
- [x] Read exactly one regular `opencode` member from ZIP without extracting
  archive paths. Reject traversal, links, special files and ambiguous binaries.
- [x] Keep archive size/digest checks, binary hashing, version probes, cache
  verification and cancellation behavior for both formats.
- [x] Test Linux and Darwin selection with mocked hosts and HTTP; exercise real
  subprocess probes using offline executable fixtures on macOS.

Scope boundary: this stage does not enable macOS agent execution or change the
Python/sandbox contract. A downloaded Darwin executable alone is insufficient.

### 2. Prove the macOS isolation boundary

- [ ] Prototype a deny-by-default Seatbelt profile via `/usr/bin/sandbox-exec`.
  Apple marks this command deprecated in its local manual. Treat availability
  and effective enforcement as runtime prerequisites, not a stable API promise.
- [ ] Test read/write/execute access for source, private HOME, runtime, temporary
  files, system libraries and explicitly configured tools. Deny the actual host
  HOME, OAuth refresh profile, Git cache/history, after revision and sibling runs.
- [ ] Test symlinks, canonical paths (`/var` versus `/private/var`), subprocesses,
  network access and filesystem enumeration. Allow reading native libraries and
  DNS/TLS inputs without granting broad access to host credentials.
- [ ] Test process signaling and host-process inspection restrictions. Document
  differences from Linux PID/user namespaces. Do not claim equivalent isolation
  if another host process can supply credentials or forbidden files.
- [ ] Gate integration on these negative tests. If the profile cannot preserve
  the required boundary on a supported macOS release, stop native execution and
  revisit the design; never weaken the profile to make a smoke test pass.

### 3. Separate launch paths from Linux mount paths

- [ ] Introduce an execution backend interface for command construction,
  environment, preflight, Python preparation/probes and cleanup. Keep bubblewrap
  behavior and existing Linux configuration defaults unchanged.
- [ ] Define backend-owned paths for source, cwd, HOME, temporary files, runtime,
  Python and OpenCode. Seatbelt cannot remap host paths to `/repo` or
  `/sandbox-home`; use a dedicated canonical attempt directory on macOS.
- [ ] Route setup, native config, declared resource/plugin URIs, MCP commands and
  CLI tools through that layout. Audit `execution/agent_input.py`, `opencode.py`,
  `python_sandbox.py`, `runtime/attempt.py` and `runtime/coordinator.py` together.
- [ ] Add an explicit macOS backend choice with early host validation. Preserve
  isolation identity in sandbox receipts and reject cross-backend resume.

### 4. Isolated macOS Python 3.12

- [ ] Discover Mach-O dependencies with `otool`, including framework builds,
  Homebrew paths, loader-relative paths, extension modules and shared libraries.
- [ ] Prototype relocation of a package-free copy of the setup interpreter.
  Account for framework lookup, loader install names and code signatures before
  choosing a supported layout. Reject unsupported installations explicitly.
- [ ] Preserve the setup interpreter as the agent base. Create a fresh private
  venv per attempt, with offline pip bootstrap; exclude host/benchmark packages.
- [ ] Replace Linux bin-directory projection with private launchers on macOS.
  Verify ordinary Python/pip calls and shebangs; reject unsupported interpreter
  entrypoints. Do not copy or modify protected macOS system command directories.
- [ ] Test ssl/sqlite3/lzma/bz2/zlib, package installation, source module shadowing,
  retries, concurrent attempts, cancellation and cleanup.

### 5. Evidence contracts and consumers

- [ ] Replace Linux-only literals in Python probes with a versioned,
  backend-aware path contract; validate against the measured attempt layout,
  rather than accepting arbitrary absolute paths.
- [ ] Update benchmark producers, schemas, validators, archive validation and the
  viewer's independent Python evidence models/validators in the same change.
- [ ] Define migration/version rules before changing persisted formats. Keep old
  Linux reports readable and do not rewrite existing baskets or completed runs.
- [ ] Ensure resume detects changed backend, Python build and runtime identity;
  ensure published evidence does not disclose credentials or private host paths.

### 6. End-to-end validation and MR readiness

- [ ] Add a hermetic fake OpenCode workflow on macOS: setup, edits, event/usage
  evidence, score, report, HTML, resume and archive import into the viewer.
- [ ] Run `make test` and `make smoke` on Linux x64 and supported macOS ARM64/x64.
  Separate host-independent tests from actual backend integration tests; do not
  globally skip failures merely because the host is macOS.
- [ ] Add CI jobs and document tested OS versions, Python installations,
  toolchain visibility and known Seatbelt limitations.
- [ ] Update prerequisites, examples, changelog and the synchronized release
  inventory when preparing the completed release.
- [ ] Prepare the MR description with behavior, isolation guarantees and exact
  validation results. The contributor must personally accept the repository CLA;
  do not add a CLA acceptance statement on their behalf.

Live model runs are outside offline validation and require separate operator
authorization. They are unnecessary to prove the initial provisioning stage.

## Validation for the first stage

From `benchmark/`, after installing the pinned dependencies:

```sh
PYTHONHASHSEED=42 .venv/bin/python -m pytest tests/test_provisioning.py
make test
make smoke
```

On macOS, full setup and Linux sandbox integration checks still have known
platform failures until later stages. Record these failures separately from the
portable provisioning results; do not report full native support based on the
first-stage tests.

Validation recorded on macOS ARM64, 2026-11-02:

- Provisioning, authentication, process, Make setup, wire contracts and wheel
  packaging tests with `PYTHONHASHSEED=42` (as exported by Make): 73 passed.
- Real official `opencode:v1.18.33`: Darwin ARM64 ZIP downloaded, published SHA-256
  verified, native `--version` passed and the second provisioning reused the cache.
  No login or model request was made.
- `make setup PYTHON=python3.12`: pinned dependencies and editable installation
  succeeded; the final Linux Python discovery failed because `ldd` is absent.
- `make test` with `PYTEST_ADDOPTS='--maxfail=1 -q'`: 12 passed, then the existing
  archive crash/recovery fixture failed during Linux Python discovery.
- `make smoke` with the same bound: 218 passed, then setup execution failed on
  missing `/usr/bin/bwrap`. Linux integration validation remains outstanding.
