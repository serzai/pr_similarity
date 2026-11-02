# Native macOS implementation plan

Status: provisioning and native launch prototypes implemented; benchmark runs
remain Linux x64/glibc only. Work is on the contributor fork `serzai/pr_similarity`,
branch `codex/native-macos-foundation`; the original repository is `upstream`.
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

- [x] Prototype a deny-by-default Seatbelt profile via `/usr/bin/sandbox-exec`.
  Apple marks this command deprecated in its local manual. Treat availability
  and effective enforcement as runtime prerequisites, not a stable API promise.
- [x] Test read/write/execute access for source, private HOME, runtime, temporary
  files, system libraries and explicitly configured tools. Deny the actual host
  HOME, OAuth refresh profile, Git cache/history, after revision and sibling runs.
- [x] Test symlinks, canonical paths (`/var` versus `/private/var`), subprocesses,
  network access and filesystem enumeration. Allow reading native libraries and
  DNS/TLS inputs without granting broad access to host credentials.
- [x] Test process signaling and host-process inspection restrictions. Document
  differences from Linux PID/user namespaces. Do not claim equivalent isolation
  if another host process can supply credentials or forbidden files.
- [x] Gate integration on these negative tests. If the profile cannot preserve
  the required boundary on a supported macOS release, stop native execution and
  revisit the design; never weaken the profile to make a smoke test pass.
- [ ] Implement supervised descendant teardown. A child that calls `setsid()`
  currently survives `run_process` process-group cleanup on macOS. The strict
  expected-failure regression `test_native_detached_descendants_are_reaped`
  demonstrates this and always kills its owned probe child. Keep native runs
  disabled until normal exit, timeout, cancellation and host death cover detached
  descendants without affecting unrelated host processes.

### 3. Separate launch paths from Linux mount paths

- [x] Add backend-owned command/environment/preflight behavior and an explicit
  process path layout. `SeatbeltSandbox` is a development prototype sharing the
  existing launcher interface. Its Python methods fail closed until stage 4.
- [ ] Complete the backend interface with native Python preparation/probes and
  descendant cleanup. Keep Linux configuration defaults unchanged.
- [x] Define backend-owned paths for source, cwd, HOME, temporary files, runtime,
  Python and OpenCode. Seatbelt cannot remap host paths to `/repo` or
  `/sandbox-home`; use a dedicated canonical attempt directory on macOS.
- [x] Route setup scripts, copied native resources, plugin URIs, agent commands,
  effective config normalization and resource evidence lookup through the layout.
- [ ] Finish native Python path handling, backend selection in the coordinator
  and explicit native MCP/tool examples; audit their ownership together.
- [ ] Add an explicit macOS backend choice with early host validation. Preserve
  isolation identity in sandbox receipts and reject cross-backend resume.

The prototype allows only dedicated writable source/HOME/temp roots and readonly
launcher resources. Read access to system code excludes `/System/Volumes/Data`.
Only ancestor metadata is allowed for path traversal, rather than a global file
metadata grant. Filesystem policy does not create a mount or PID namespace.
IP networking, including loopback, is deliberately shared as on Linux; it is not
a barrier against a cooperating host HTTP service. Arbitrary host Unix sockets
are denied, with only the system DNS socket explicitly allowed. Native temporary
files currently live in a private attempt directory, not a Linux tmpfs.

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

## Native prototype validation

On macOS 26.1 ARM64, the Seatbelt/path tests passed with the real pinned binary:
33 passed and one strict expected failure for detached descendant cleanup.
The tests cover parameter-safe Unicode/quoted paths, negative file access,
symlink and Data-volume aliases, readonly Git/runtime, restricted host process
inspection/signals, shared IP networking, denied host Unix sockets, native cwd,
frozen resources, setup, fake-agent events, effective config and cancellation.
Real OpenCode `--version` and `debug config` ran inside Seatbelt without model
credentials. A separate bounded HTTPS probe verified DNS/TLS.

The portable regression group (provisioning, agent inputs, attempt execution,
auth, process lifecycle, setup, wire contracts, wheel packaging, config and Make)
passed 140 tests. The scheduler integration still fails on macOS at the existing
bubblewrap provisioning gate. No Python evidence or saved-run schemas were
changed in this prototype.

The updated `make smoke` (bounded with `--maxfail=1`) reached 252 passed,
one skipped optional real-binary check and the known descendant-cleanup xfail,
then stopped at the existing `/usr/bin/bwrap` setup test. `make test` still stops
at the Linux Python discovery used by the archive crash/recovery fixture.

Run these checks from `benchmark/` on an ordinary macOS terminal:

```sh
PYTHONHASHSEED=42 .venv/bin/python -m pytest tests/test_seatbelt.py tests/test_execution_paths.py -q -rx
```

Set `BENCHMARK_TEST_OPENCODE` to an already provisioned v1.18.33 executable to
also exercise the optional real-binary smoke. These checks are included in
`make smoke`. A parent application sandbox that forbids nested Seatbelt may
produce `sandbox_apply: Operation not permitted`; that is a failed prerequisite,
not a reason to silently run the command without isolation.
