# Functional validation implementation

The existing baseline measures source-change similarity. The opt-in tested
profile must additionally prepare the target project and record an independently
executed test phase. Existing frozen runs and the baseline retain their semantics.

Acceptance criteria:

- A content-bound setup script installs dependencies and builds the exact before
  source inside isolation. A failed import or collection check prevents model use.
- Native compilation uses an explicit measured readonly Apple toolchain; host
  HOME, credentials, Git caches and after revisions remain inaccessible.
- After the agent, capture its source changes before validation can write files.
  Execute a content-bound validation script inside the same private sandbox.
- Preserve command status, timeout/cancellation, log and a bounded JUnit report.
  Require at least one executed test. Missing/invalid XML, all-skipped tests and
  nonzero commands cannot produce a passing result.
- Keep execution, tests and MLP similarity separate in sealed facts, report,
  summary, console and viewer. Failed required tests affect final run success.
- Bind test configuration and evidence on resume/archive import. Historical
  absent test fields remain absent and do not become invented passes or failures.
- Validate pass/fail/timeout/cancel, partial reports, path escapes and crashes
  through the producer and independent viewer; then exercise a real pandas task.

Implementation order: test-result parsing and runner; artifact/config contracts;
attempt lifecycle and consumer validation/UI; measured native toolchain; pandas
setup/validation profile; end-to-end and real-task verification.

Test success covers the configured project tests. It is separate from MLP and
does not imply hidden-test correctness; the current baskets contain no held-out
SWE-bench-style FAIL_TO_PASS test specification.
