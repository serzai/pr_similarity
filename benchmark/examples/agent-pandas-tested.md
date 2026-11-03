The project was built from this before-only checkout into your private Python
environment. Use `python` and `python -m pytest`; imports resolve to this source.

For functional source changes, add or update an appropriate regression test and
run relevant tests while implementing the task. Resolve failures before stopping.
The harness independently runs every changed/new `pandas/tests/**/*.py` file
after you finish. A code change without a changed regression test fails this
profile's validation; documentation-only changes receive a project API smoke.

Preserve existing coverage. Do not delete tests, weaken assertions or add skips
to conceal failures. Missing dependencies, failed collection and all-skipped
populations are not successful tests. Report any actual limitations explicitly.

Keep all implementation and test changes in this workspace. Do not access the
original pull request or reference solution, alter Git metadata, or replace the
project with a downloaded pandas wheel.
