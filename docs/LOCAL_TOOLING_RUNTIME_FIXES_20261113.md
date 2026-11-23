# Local tooling runtime fixes — 2026-11-13

Four runtime problems in the local development tooling are corrected:

1. Durable compile retries identify the original caller arguments and stable function ID,
   rather than regenerated diagnostic source snapshots. Old saved request records follow
   the same comparison. Identical retries return the original result after edits or a
   function rename; changed arguments remain rejected.
2. Two bounded workers can execute independent source units. Jobs sharing one source
   file serialize without tying up the other worker. Existing tree/unit locks protect
   shared mutations. A heartbeat renews only existing, unexpired claims owned by active
   operations in this process; it never revives released or reassigned claims.
3. Near-match finishing caches only judged misses. A compiler or comparison failure can
   be retried at the same revision instead of being suppressed as an already-tried trial.
4. Block and pattern donor lookup share bounded target digest metadata. Identity, size,
   mtime and ctime changes invalidate it; recently modified targets are always reread,
   and a 30-second maximum age forces periodic refresh. Decoded blocks also track target
   content digests. This advisory cache never supplies a judge verdict.

Regression tests cover stable retry identity and legacy records, function renames,
independent versus shared-source scheduling, the worker bound, queued/running lease
renewal, lost ownership, failed-trial recovery, object replacement, preserved timestamps,
read-time changes, cache expiry and cache bounds. A compiler-backed PS4 test submits
an incorrect definition, applies an exact patch through the durable worker, verifies a
strict match and released claim, then retries the original request without replay.

A synthetic benchmark used 2,574 homemade 8 KiB files (21,086,208 bytes). Initial
lookup read all 2,574 objects in 0.1536 seconds; the unchanged repeat read zero object
contents in 0.0301 seconds. Both passes still check metadata. These are isolated donor
cache timings, not a measured end-to-end decompilation speedup.

Testing uses an isolated checkout, scratch app homes, pinned tools and a separate Python
environment. Installation preserves custom local sources and backs up the original
files. No live project, database, running process or app environment is modified.

Validation: the clean full suite completed with **1,885 passed, 396 skipped and three
pre-existing checks excluded** (the system GTK binding check and two filesystem-sensitive
ROM-library cache checks). Ruff across src/tests/projects and the whitespace check passed.
The focused runtime-tooling suite passed 99 tests with one skip; block inspection passed
15 tests. The full run includes the compiler-backed PS4 workflow and durable compile test.

The pre-install backup and changed-file manifest are stored at
`/home/Trist/dev/backups/decompdecomp-before-tooling-runtime-fixes-20261113`.
Restart the development app when ready to load the updated Python modules; the running
process is deliberately left alone.
