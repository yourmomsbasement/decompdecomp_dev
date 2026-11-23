# Local build review — 2026-11-13

The installed sources matched the previous tested integration at the start of this
review. The source delta against upstream main is 77 files (10,091 added lines and 1,260
removed lines, including this review's code changes). It is the retained local
development tooling, not drift from that installation. This update preserves those APIs, the
ordinary strict judge, and the prior compile/build-error crash fix.

Changes carried into this local copy:

- Compiler revisions include only compiler selectors, the entire unit, local headers,
  tools/config/harness inputs, build files and the target. Background bookkeeping
  no longer invalidates revisions. Input reads and scans are bounded; external
  header links are skipped explicitly rather than disabling the tools.
- Briefs and unlinked trials use a shared tree lock plus their own unit lock.
  Linked-game trials still run exclusively. Applying a recipe releases the batch
  lock, reacquires the appropriate unit locks, rechecks its revision and uses the
  ordinary judge. The local explicit CPU recipes and experiment records remain.
- Local previews restore the original source bytes, including CRLF, and ignore
  expired foreign claims without changing claim records.
- Assembly stubs direct the helper to compile_and_diff before requesting a patch.

Regression coverage includes the new PR's review cases and local durable operations,
regions, specialists, part claims and the previous unbound-diff crash regressions.
The full suite and compiler-backed PS4 workflows run in an isolated copy. No live
project, database, running process or environment is altered during testing.

PR #280 adds dispatch tests for GC, PC, Xbox and DS as well as PS4. The other four
need proprietary compiler packs absent from this environment, so their skips are
reported rather than presented as successful demo executions.

Validation: full local suite 1,860 passed, 396 skipped, three known environmental
checks excluded; latest compile-tool unit suite 62 passed, one compiler-dependent
check skipped; ruff and whitespace checks passed. The full run includes the real
pinned-compiler PS4 workflow tests and the prior normal-compile crash regressions.
