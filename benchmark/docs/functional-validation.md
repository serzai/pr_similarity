# Required project tests

The default baseline measures similarity to human source changes. To also require
project tests, configure `agent.validation` with a script and timeout. These are
separate measurements: a high similarity score does not imply passing tests.

## Native pandas recipe

Install Apple's Command Line Tools (`xcode-select --install`) and complete the
[native prerequisites](macos.md). From `benchmark/`, run:

```bash
make run CONFIG=examples/pandas-macos-tested.yaml \
  DATASET=examples/baskets/pandas/pandas-2026H1-1.json \
  OUTPUT=./output WORKERS=1
```

For the five-task basket, use `pandas-2026H1-5.json` instead. Start with one worker:
each attempt builds pandas and may run a large test file. This profile has the
`pandas-tests` tag and additional agent instructions; compare it with runs using
the same profile. It does not change `examples/macos.yaml` or baseline results.

The setup installs exact, hash-verified build/test wheels in the attempt's private
venv, then builds the exported before source with Meson in editable mode. It
checks the import location, a datetime operation and pytest collection before
starting the agent. NumPy is pinned to 2.2.6: the tested historical source crashes
in datetime conversion with 2.5.4. No prebuilt pandas wheel replaces the source.
Dependencies and test selection can need changes for another project or revision;
edit the recipe and start a new run.

`sandbox.toolchain: apple_clang` explicitly enables a measured private copy of
Apple's compiler, linker, compiler resources and selected SDK. Preflight verifies
its digest and compiles, links and executes a C++ program inside Seatbelt. The
snapshot is readonly to target code; the original Xcode tree and host package
prefixes remain inaccessible. The isolation receipt records its content digest.
Allow disk space for the SDK snapshot and each source/build/venv. Linux rejects
this Apple-specific setting; its validation scripts use the existing sandbox.

## Test selection and evidence

After OpenCode stops, the runner captures source changes for scoring, then runs
the frozen validation script in the same isolated attempt. Test side effects do
not become agent edits in the saved diff. Provider credentials are not forwarded
to the validation command. `PRSIM_TEST_REPORT` supplies a random private JUnit
destination; scripts must write their test runner's XML there and preserve its
exit status.

The pandas recipe runs complete changed/new `pandas/tests/*.py` files. Functional
source changes without changed regression coverage fail validation. For changes
outside functional source, it runs the project's API smoke tests. It resolves the
Git root even when the task starts in a subdirectory. It does not run pandas'
entire suite or provide held-out tests: the frozen baskets contain no
SWE-bench-style FAIL_TO_PASS specification. Agents can edit project tests, so a
pass means the configured checks passed, not independent hidden-test correctness.

Each task's optional `tests` result contains status, exit code, duration, actual
testcase counts, `tests.log`, a size/SHA-256 receipt for `tests.xml`, and a reason
when available. Counts distinguish passed, failed, errors and skipped tests.
The producer and viewer independently recount XML cases, validate artifact
ownership and reject inconsistent receipts. Empty/all-skipped reports, missing
or unsafe XML, and nonzero commands cannot pass.

Statuses are `not_started`, `passed`, `failed`, `error`, `timeout` and `cancelled`.
Partial XML on timeout/cancellation remains evidence, without overriding that
status. Required nonpassing tests make the run `completed_with_errors` and its
command return nonzero even when agent execution and similarity scoring succeed.
Console, report, viewer and standalone HTML retain these separate results.
Historical runs without validation retain their absent test field.

## Resume and failures

The validation script bytes and timeout participate in frozen configuration
identity, alongside setup and declared resources. A changed recipe requires a
new run; an old baseline cannot acquire tests through resume. Completed test
facts belong to the immutable attempt seal and survive archive import and
host-only score recovery. A coordinator crash before sealing validation leaves
an incomplete attempt; normal resume may need a new native attempt. A failed
test is a recorded result, not an automatic retry of an otherwise completed
agent. Inspect `tests.log` and the selected attempt before deciding to rerun.
