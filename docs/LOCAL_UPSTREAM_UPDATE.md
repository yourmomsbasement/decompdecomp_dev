# Local development build update — 2026-11-12

This working copy incorporates upstream main at `6b2531851586304499b850e1d2fde7e1e1c4b0f8`
(version 1.0.10-alpha.1), while retaining the local compile operations, recipe previews,
experiment history, function parts, specialist reports, compiler diagnostics, region
work, tiny-function tools, deep permuter, per-unit compilers and PS4 TLS support.

The focused upstream contribution is https://github.com/n-delic/decompdecomp/pull/280.
The local tools retain their additional APIs and explicit architecture recipes; they
are a larger development implementation than the focused contribution.

The ordinary compile now uses the current upstream judge, with local part-claim
protection and candidate fingerprints retained. Previews and part compiles keep
the local transactional implementation, including the earlier build/diff crash fix.
Candidate fingerprints are captured before undoing an unsuccessful candidate.

Integration repairs include the shared console interfaces, compiler registration,
helper account/session migrations, claim scheduling, progress guards, function-view
controls, PS4 importer fixes and original function addresses. Cached Ghidra drafts,
file splitting, and local helper tools remain available. No user project, database,
installed compiler, or active process is changed by installing these source files.

Validation: 1,849 passed, 396 skipped, three known host/environment checks excluded;
ruff and whitespace checks passed. The real PS4 workflow subset passed all six checks.

Validation results and the full before-image are stored beside the isolated
integration checkout and in the installation backup. Restart the dev app normally
to load the updated Python modules; its running process is not restarted by this update.
