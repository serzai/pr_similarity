# Native macOS implementation plan

Status: the explicit experimental Seatbelt backend is integrated into the
coordinator and attempts. Native offline setup, fake-agent execution, scoring,
resume and archive import work on macOS 26.1 ARM64/python.org Python 3.12.0.
Linux remains the production baseline. Hosted Linux x64 and macOS 15 ARM64 have
passed; the complete Intel matrix and Homebrew validation are still open.
Work is on the fork `serzai/pr_similarity`, branch
`codex/native-macos-foundation`; the original repository is `upstream`.
Use [native setup](macos.md) for the experimental run configuration.

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
- [x] Implement supervised descendant teardown using an ephemeral launchd job
  with its own kernel resource coalition. Freeze and kill only that coalition,
  using PID-version-aware audit-token signals. A guardian outside Seatbelt
  survives the coordinator/client and removes its temporary job afterwards.
  Regressions cover normal exit, timeout, cancellation, callback exceptions,
  coordinator/client/guardian SIGKILL, double forks, `setsid`,
  `POSIX_SPAWN_SETSID` and `POSIX_SPAWN_SETPGROUP`; an unrelated process survives.
  Environment and stdio descriptors cross a private socket in memory, rather
  than storing provider credentials in launchd property lists.
  A syscall deny for `setsid`/`setpgid` was previously tested and did not contain
  the `posix_spawn` variants. The macOS SDK also marks kqueue `NOTE_TRACK`,
  `NOTE_TRACKERR` and `NOTE_CHILD` unsupported since 10.5 despite Python exposing
  those constants. These approaches are not used.

### 3. Separate launch paths from Linux mount paths

- [x] Add backend-owned command/environment/preflight behavior and an explicit
  process path layout. `SeatbeltSandbox` owns native paths and lifecycle throughout
  coordinator preflight, setup, diagnostics, Python probes and agent attempts.
- [x] Complete the backend interface with native Python preparation/probes and
  descendant cleanup. Setup, diagnostics and agent launches use backend-owned
  execution. Keep Linux configuration defaults unchanged.
- [x] Define backend-owned paths for source, cwd, HOME, temporary files, runtime,
  Python and OpenCode. Seatbelt cannot remap host paths to `/repo` or
  `/sandbox-home`; use a dedicated canonical attempt directory on macOS.
- [x] Route setup scripts, copied native resources, plugin URIs, agent commands,
  effective config normalization and resource evidence lookup through the layout.
- [x] Integrate native Python paths and backend selection in the coordinator.
- [ ] Add explicit native MCP/tool bundles beyond the private Python/Git runtime.
- [x] Add an explicit macOS backend choice with early host validation. Preserve
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

- [x] Discover private Mach-O dependencies with `otool`, including framework
  builds, `@loader_path`, `@executable_path`, `@rpath`, extensions and transitive
  shared libraries. Rpaths are anchored to the binary owning each load command.
- [ ] Validate real Homebrew and Intel installations; only python.org framework
  Python 3.12 on macOS 26.1 ARM64 has been tested so far.
- [x] Prototype relocation of a package-free copy of the setup interpreter.
  Account for framework lookup, loader install names and code signatures before
  choosing a supported layout. Reject unsupported installations explicitly.
- [x] Preserve the setup interpreter as the agent base. Create a fresh private
  venv per attempt, with offline pip bootstrap; exclude host/benchmark packages.
- [x] Replace Linux bin-directory projection with private venv entrypoints on macOS.
  Verify PATH-based Python, `python -m pip`, and `#!/usr/bin/env python`; deny the
  protected absolute system Python. System command directories are not modified.
- [ ] Validate direct `pip`/`pip3`, other interpreter entrypoints and shebangs on
  both supported architectures before advertising the complete command contract.
- [x] Test ssl/sqlite3/lzma/bz2/zlib, offline wheel installation, source module
  shadowing, separate attempt packages, deterministic snapshots and preparation
  cancellation. Verify Python setup, fake-agent events and durable native probes
  together, including paths with spaces, Unicode and quotes.
- [x] Validate resumed fresh attempts, two simultaneous native workers, separate
  venv/source/usage and complete archive evidence through the real coordinator.
- [ ] Validate compiled target dependencies and SDK/compiler tool bundles.

### 5. Evidence contracts and consumers

- [x] Extend Linux-only Python probes with a versioned,
  backend-aware path contract; validate against the measured attempt layout,
  rather than accepting arbitrary absolute paths.
- [x] Update the owning Python probe models and producer/consumer validators.
  Probe version 2 uses portable native location identifiers and is bound to the
  runtime platform; mixed path families are rejected.
- [x] Define migration/version rules before changing persisted formats. Keep old
  Linux reports readable and do not rewrite existing baskets or completed runs.
- [x] Compare backend, policy/OS/Git identity, Python platform/content and pinned
  runtime before resuming native work. Normalize private native paths in logs,
  events, launch metadata and legacy resource copies; cover Unicode/JSON escaping
  and retain credential checks before alias transformation. Public archives pass
  the independent viewer validator and local mock API. Operator-declared config
  paths and host DEBUG traceback locations retain the existing archive contract;
  this is not a claim that every absolute operator path is removed.

### 6. End-to-end validation and MR readiness

- [x] Add a hermetic fake OpenCode workflow on macOS: setup, edits, event/usage
  evidence, score, report, HTML, resume and archive import into the viewer.
- [ ] Run `make test` and `make smoke` on Linux x64 and supported macOS ARM64/x64.
  Separate host-independent tests from actual backend integration tests; do not
  globally skip failures merely because the host is macOS.
- [x] Add CI jobs for Linux x64, macOS ARM64 and macOS Intel, pinned to exact
  official action commits; document tested installations and toolchain limits.
- [ ] Obtain successful results from all hosted-runner jobs; configuration alone
  is not validation of Intel, Homebrew or headless launchd behavior.
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

The launch-prototype `make smoke` (bounded with `--maxfail=1`) reached 252 passed,
one skipped optional real-binary check and the known descendant-cleanup xfail,
then stopped at the existing `/usr/bin/bwrap` setup test. `make test` still stops
at the then-Linux-only Python discovery used by the archive crash/recovery fixture.

## Native Python and versioned probe checkpoint

Native preparation passes the final `make setup` interpreter check, including
offline creation of a clean venv with pip. It copies the real framework interpreter,
rewrites library paths only in private copies, ad-hoc signs them, and publishes a
content-verified snapshot. Repeated preparation produces the same receipt. Host
and benchmark packages are absent from the attempt environment. Linux's probe
script remains byte-for-byte unchanged.

The native isolation/contract group passed 58 checks with the optional real
OpenCode smoke enabled. The three strict expected failures all belong to the
outstanding detached-descendant cleanup problem. Consumer model/archive/
inference tests passed 86 checks, including historical Linux fixtures. Full
viewer tests also include browser setup and Linux daemon lifecycle behavior;
they are not a claim of native viewer-server support.

An additional regression covers creating the per-attempt temporary directory
without reusing the coordinator's disposable preflight. Its host-independent
layout check and real Seatbelt bootstrap both pass on macOS 26.1 ARM64.

With native Python checks added, bounded `make smoke` reached 277 passed,
one optional real-binary skip and three known cleanup xfails, then failed on the
existing `/usr/bin/bwrap` setup test. Bounded `make test` reached 12 passed and
now stops at the Linux sandbox gate rather than `ldd` discovery. No model request
or publication was performed.

Run these checks from `benchmark/` on an ordinary macOS terminal:

```sh
PYTHONHASHSEED=42 .venv/bin/python -m pytest tests/test_seatbelt.py tests/test_execution_paths.py -q -rx
```

Set `BENCHMARK_TEST_OPENCODE` to an already provisioned v1.18.33 executable to
also exercise the optional real-binary smoke. These checks are included in
`make smoke`. A parent application sandbox that forbids nested Seatbelt may
produce `sandbox_apply: Operation not permitted`; that is a failed prerequisite,
not a reason to silently run the command without isolation.


## Supervised native lifecycle checkpoint

Native setup, Python preparation/probes, effective-config diagnostics and agent
launches now use `Sandbox.run_process`. Linux retains its existing command and
process-group behavior. Native execution delegates output streaming/redaction to
the same implementation while an independent launchd guardian owns the payload.
The guardian refuses a coalition shared with the coordinator; atomic PID-version
checks reject reused identities. Launchd GUI/user domain availability and the
private libproc operations are required; absence fails without issuing an agent.
GUI-domain operation is tested on macOS 26.1 ARM64; headless user-domain operation
and older macOS versions still need validation.

The previous three strict cleanup xfails are now ordinary passing tests, with a
fourth case for a double fork. Twelve supervisor tests cover exit status, stdio,
redaction, cancellation, timeout, callback failure, coordinator/client/guardian
crashes, PID-version mismatch, concurrent invocations, job/control-directory
cleanup, and credentials absent from launchd files. The process/launcher/path
regression group passed 50 checks with two platform/optional skips before the
last two supervisor regressions were added; those twelve supervisor tests pass.
No model request or publication was performed.

Remaining integration work is explicit backend selection in the coordinator and
attempt factory, portable public launcher/log evidence, then an offline full
run/resume/archive round trip and Linux regression validation. The regular runner
continues to select bubblewrap until that integration is complete.


The full smoke at this checkpoint reached 463 passed, 3 skipped, 73 failed and
25 fixture errors. Three quota-stop mocks still patched the former module-level
launch function; they were moved to the backend method and rerun successfully.
The remaining failures require Linux bubblewrap/system-bin projection or runner
integration through the still-Linux coordinator. Bounded `make test` again reached
12 passed before the archive crash/recovery fixture failed at bubblewrap
provisioning. These full-suite failures remain open; native lifecycle checks do
not substitute for Linux or full-run validation.


## Coordinator and public evidence checkpoint

`backend: seatbelt` now selects the native class in both coordinator and attempt
execution. Linux defaults are unchanged, wrong-host selection fails before
credentials/provisioning, and Seatbelt requires the system executable without
auto-installation. A new focused example and native guide describe actual paths,
display aliases, PATH-based Python/Git entrypoints and remaining SDK/tool limits.

The full offline workflow passed for basket v1/v2/v3, including setup, agent edits,
SQLite usage, scoring, moving a completed run, resume, rescore/reprice and archive
verification. The archive test rejects leaked attempt/preflight path prefixes.
Five SIGKILL/recovery boundaries passed for basket v3. After adding private Git,
the current-basket workflow and live-agent SIGKILL/recovery both passed again.
Native Python lifecycle checks passed all 11 tests, including bootstrap timeout,
cancellation, no inherited packages and disposal of owned processes/HOME. Fifty
scheduler/backend tests pass with explicit sandbox substitutes for scheduler-only
fixtures; actual isolation remains covered by integration tests.

A real Git check caught Apple's `/usr/bin/git` shim requiring inaccessible Xcode
paths. Native preparation now copies only the actual Apple Git binary, HTTP
helpers and builtin entrypoints, verifies system-only Mach-O dependencies and
content-addresses the snapshot. Each attempt receives readonly copies and private
Git environment controls. The snapshot digest is part of the sandbox identity,
so an updated toolchain cannot silently resume earlier native work. No developer
or Homebrew directory was added to the sandbox read allowlist.

The independent viewer accepted a newly generated macOS archive, published it to
local mock storage and served all base/human/agent role-pair APIs. Historical and
probe/model/archive checks also passed. No live model request, external account
change or external publication was performed. Full Make/CI results are recorded
below once available; Linux-only namespace tests are explicitly marked, while
portable cache/contract/scheduler tests continue to run on macOS.


ARM64 validation on the integrated backend:

- `make smoke`: 545 passed, 37 skipped (Linux namespace/bin-projection cases and
  optional real-binary checks). No failures.
- Full `make test`: 850 passed, 37 skipped, one failure in a bubblewrap fallback
  unit fixture that omitted its Linux host assumption. The fixture now explicitly
  models Linux x64; all 12 tests in that module pass on rerun. This is not a claim
  of a subsequently repeated all-green full suite.
- Two real concurrent coordinator attempts: passed with separate source/venvs,
  16 tokens and synthetic USD 0.5 per attempt, and a complete validated archive.
- A bounded `git ls-remote` against the public contributor fork succeeded inside
  Seatbelt using the private HTTP helper without host Git credentials. This was
  separate from offline fixtures; no model request or write to GitHub occurred.
- Native path tests: 23 passed, one optional real-binary skip; include JSON-escaped
  Unicode/quotes, parent-directory aliases and secret checks before aliasing.

CI is configured for full regression/smoke and independent archive-consumer checks
on Ubuntu 24.04 x64, macOS 15 ARM64 and macOS 15 Intel. Actions are pinned to the
verified v7 commits. Runner labels follow the
[GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
The initial integrated hosted results and subsequent fixes are recorded below.

## Hosted validation and native TLS checkpoint

At `6abd950`, the full local macOS 26.1 ARM64 suite passed **864 tests, 37 skipped**.
The [hosted matrix](https://github.com/serzai/pr_similarity/actions/runs/41533524038)
completed successfully on Ubuntu 24.04 x64 and macOS 15 ARM64:

- Linux: regression 858 passed/43 skipped, smoke 548 passed/43 skipped,
  exact Python provenance and independent archive consumer passed.
- ARM64: regression 864 passed/37 skipped, smoke 554 passed/37 skipped,
  five crash/recovery boundaries and 61 independent consumer checks passed.
- The Intel job is still pending; its earlier run reached 853 passed/37 skipped
  with two consumer failures caused by missing viewer setup. Setup now runs
  before producer/consumer conformance tests.

Linux CI exposed two prerequisites now handled explicitly: Ubuntu's scoped
AppArmor user-namespace permission for bubblewrap, and hosted Python's private
ELF library selection. The runner copies and measures selected private libraries
and verifies the exact interpreter patch version; it never forwards host loader
paths into the sandbox.

A separate native HTTPS check found that relocated OpenSSL still searched the
original host framework for CA certificates. Native environments now default
`SSL_CERT_FILE` to the already-allowed system bundle. Certificate/hostname checks
remain enabled and explicit private CA bindings are retained. The offline trust
regression and Python/backend group passed 23 tests; a bounded read-only HTTPS
request to the contributor fork loaded 128 CAs and returned 200. Sandbox identity
changes with this environment policy so resume cannot silently switch defaults.
