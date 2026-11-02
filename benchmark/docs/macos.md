[PR Similarity](../../README.md) / [benchmark](../README.md)

# Native macOS execution (experimental)

The benchmark can run directly on macOS with `sandbox.backend: seatbelt`.
Validated hosts include macOS 26.1 ARM64 with python.org Python 3.12.0 and the
macOS 15 ARM64 GitHub runner with Python 3.12.10 and Apple Command Line Tools.
Homebrew Python and the complete Intel matrix still need independent validation.
Linux remains the production
baseline; the collector and viewer server retain their existing platform support.

## Prepare and run

Install Python 3.12, Make, Git and Apple's Command Line Tools. Native preparation
uses `xcrun`, `otool`, `install_name_tool` and `codesign`; it copies and signs private
runtime files. `/usr/bin/sandbox-exec` and safe libproc process operations must be
available. The runner probes enforcement and private Python before issuing tasks.
It stops if a required native isolation operation is unavailable.

From `benchmark/`, prepare the ordinary module environment and choose the explicit
native example:

```sh
make setup PYTHON=python3.12
make auth CONFIG=examples/macos.yaml AUTH_FLOW=headless
make run CONFIG=examples/macos.yaml DATASET=/path/to/basket.json OUTPUT=./output
```

The auth command logs into your private ChatGPT OAuth profile. To use an API key,
copy [openai-api-key.yaml](../examples/openai-api-key.yaml) and add:

```yaml
sandbox:
  backend: seatbelt
  executable: /usr/bin/sandbox-exec
  auto_install: false
```

The backend-specific executable and `auto_install: false` are also the defaults
when `backend: seatbelt` is explicit. An omitted backend retains Linux bubblewrap;
there is no automatic switch or unsandboxed fallback. The system Seatbelt binary
is required, rather than a downloaded substitute.

Normal [resume](reference.md#resume-retries-progress-and-logging), rescore,
reprice, HTML and archive commands are unchanged. Reissuing an attempt requires
the original Python platform/content and sandbox identity, including the Git
snapshot digest. Completed saved facts can be reused after moving a run without
replaying native execution. Keep original frozen basket bytes.

## Runtime and isolation

Each attempt sees its before-only source, private HOME/temp, readonly launch
resources, a clean venv and a private Apple Git snapshot. The actual process paths
are canonical native paths. Saved logs/events display `/repo`, `/sandbox-home`,
`/benchmark-runtime`, `/benchmark-bin/opencode` and `/benchmark-python` aliases;
the common private parent uses `@attempt`. These aliases are not filesystem mounts. Python probe v2 records portable
`@attempt`/`@runtime` locations and is checked by both producer and viewer.

Apple's `/usr/bin/git` is a developer-tools shim. The launcher instead copies the
selected Git executable, HTTP transport helpers and builtin entrypoints into a
content-verified cache and the readonly runtime. Their content digest is part of
the sandbox receipt. This permits Git status/diff without exposing the developer
tools tree. Host Git configuration and credential helpers are not inherited.

Private Python uses `/private/etc/ssl/cert.pem` as its default CA bundle, with
certificate and hostname verification enabled. This avoids OpenSSL looking in
the original Python installation after relocation. Setup can explicitly select
a CA file inside its private directories through `SSL_CERT_FILE`.

Use `python`, `python3`, `pip` and `git` from PATH in setup scripts and tools.
Absolute system Python is denied; absolute `/usr/bin/git` still invokes Apple's
shim. Unlike Linux bind mounts, Seatbelt cannot redirect these absolute paths.
Host Homebrew programs, compilers/SDKs, GUI frameworks and Xcode trees are not
implicitly exposed. Building compiled target dependencies and native MCP/tool
bundles needs further toolchain support; use supported private installations and
binary wheels where appropriate. Linux setup scripts using apt, GNU-only flags
or `/repo` literals need platform-specific equivalents.

The policy denies host HOME, sibling attempts, original Git cache/history and
after-revision files. Readonly Git metadata remains protected from writes and
hardlink escapes. Processes run as the current UID, without Linux PID/user
namespaces. IP networking, including loopback, is shared; arbitrary host Unix
sockets are denied except the system DNS socket. Seatbelt is deprecated by Apple,
so enforcement is a checked prerequisite rather than a promised future OS API.

Every native phase runs under an ephemeral launchd guardian in a dedicated
resource coalition. It freezes and kills owned descendants, including double
forks and new sessions/groups, using PID-version-aware signals. The guardian
survives coordinator/client failure; the coordinator can recover cleanup after
a guardian crash. Environment values and stdio descriptors travel through a
private in-memory socket, not launchd property-list credentials. Jobs and control
files are removed after teardown.

## Offline validation

The integration fixtures use real Git, private Python, Seatbelt, native SQLite,
scoring and archive validation with a provider-free executable. They make no model
request. Native supervisor tests cover normal exit, timeout, cancellation, callback
failure, detached processes, concurrent invocations and SIGKILL. Crash/recovery
fixtures exercise reservation, live agent, seal, outcome and state boundaries.

```sh
PYTHONHASHSEED=42 .venv/bin/python -m pytest \
  tests/test_backends.py tests/test_seatbelt.py tests/test_macos_supervisor.py \
  tests/test_macos_git.py tests/test_execution_paths.py tests/test_macos_python.py \
  tests/test_artifact_workflow.py
```

The [implementation plan](native-macos-plan.md) records exact checks, remaining
Linux/macOS matrix work and MR readiness. A passing offline fixture does not
assert that a particular target project's dependency stack builds on macOS.
