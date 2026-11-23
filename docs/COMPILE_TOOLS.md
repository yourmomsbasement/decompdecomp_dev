# Smaller compile requests and recipe trials

These agent tools work on source-tree projects and public forks through both MCP and the built-in API helper.
They use the project's existing compiler and strict objdiff configuration. They do not change compiler binaries,
project flags, or judge settings. ROM support is tracked in ROADMAP.md.

Start with `claim_function`, then `function_brief(function="work")`. The compact brief contains the current
function definition once, its saved best score, and a revision. `get_function` still returns assembly, notes and
other context; it omits a second copy of the best source when that equals the current definition.

Send a small edit instead of the entire definition:

```json
{
  "function": "work",
  "revision": "<revision from function_brief>",
  "edits": [{"old": "return x + 1;", "new": "return x + 2;"}]
}
```

`compile_patch` applies these replacements in order. Each old string must occur exactly once. Definitions must
already exist; add a new one with `compile_and_diff`. Shared declarations, includes, macros and other functions
belong in `edit_file`, where the existing guards apply. The response limits long diff output and returns the new
revision; `last_diff` retains the full comparison.

`try_recipes` evaluates alternatives from the same starting definition, rather than accumulating edits:

```json
{
  "function": "work",
  "revision": "<revision from function_brief>",
  "recipes": [
    {"name": "btver2"},
    {"name": "no-sse"},
    {"name": "alternate spelling", "edits": [{"old": "return x + 1;", "new": "return 1 + x;"}]}
  ],
  "apply": false
}
```

The two named presets are x86 compiler experiments: a function-local `target("arch=btver2")` attribute tests
Jaguar scheduling, and `target("no-sse")` tests integer copy expansion. Unsupported compilers or incompatible
floating-point/ABI requirements can reject them. No preset changes shared declarations. Visibility or branch
fence experiments can be supplied as exact edits where they fit entirely within the function; shared visibility
changes still require `edit_file`.

There are at most eight recipes per call, each with at most sixteen replacements and a 64 KiB resulting source
limit. The baseline and every preview use the normal build and strict diff. Every currently matched neighbor
must survive; a matched function itself must also remain exact. A matching function in a unit linked into the
game must pass the existing full-game verification. An unmeasurable baseline stops the operation.

Preview restores the original source and rebuilds it in `finally`, even after an exact match or a diff exception.
It does not record attempts, release/renew claims, change saved best source, commit, or share trials with a room.
A restoration build failure is reported explicitly. With `apply=true`, only a strictly lower-scoring winner goes
through the ordinary keeping attempt, which rechecks matches, records progress and commits/shares a match as usual.
Equal scores leave the file alone. A preview finding a match therefore still needs an applying call to accept it.

Briefs and unlinked trials hold the shared tree lock and their unit lock; other units can build alongside them.
Linked-game trials still hold the exclusive tree lock. A recipe batch releases its locks before applying a winner,
then rechecks the revision under the current unit locks and uses the ordinary judge. Revisions include the whole
unit text, compiler selectors, local headers/tools/config/harness files, build configuration and target object.
Background bookkeeping and unrelated generation counters are excluded. Directory scans are capped at 20,000
entries and input reads at 128 MiB. External header links and generated/ignored directories are excluded from the
revision. Avoid editing the project externally while it builds; these locks coordinate app operations, not an
external editor. Previews restore original source bytes, including CRLF. Run experimental tests on a copy.

Regression tests use synthetic verdicts without requiring a compiler. To exercise a real compiler and strict
objdiff too, set `DECOMP_TEST_CLANG` to an explicitly selected installed Clang binary and run
`pytest tests/test_compile_tools.py`; the integration test skips unless that path and pinned objdiff are available.


## Big functions in parts

For functions of 400 instructions or more (`function_parts.LARGE`). `list_parts(function)` cuts the target into
parts of 60-240 instructions at block boundaries: a block only branches reach (a case, the code after a return) from
60 instructions on, any block from 150, and a forced cut at 240 inside one long block. `list_parts(function, part=N)`
shows that part's assembly, its rows of the latest diff, and the C with the other parts folded to a line count.

The C marks each part with comments, which change no code: `/* part N */ ... /* end part N */`, in order, neither
overlapping nor nested. Mark the parts in the current C (it keeps its score) or, for a function with no C yet, send a
skeleton with compile_and_diff first (prologue, branches or switch with empty parts, epilogue).
`compile_part(function, part, code)` replaces only the text between part N's markers, then goes through the normal
judge (`ForkEngine.attempt`, recorded with `via='part'`): matched neighbours, the linked game, the best-so-far rule
and the match verdict are unchanged. The answer is the verdict, that part's diff rows and each part's count of
differing rows (rows are mapped to target instructions; an extra instruction goes with the target instruction after
it). Declarations, types and macros still go in the file with edit_file.

`claim_part(function, part=0)` holds one marked part for 30 minutes (each compile_part renews it) beside whoever
holds the function; with no part given, the free part with the most differing rows. An agent holds one part at a
time and no other function. While another agent holds the function or a part of it, compile_part previews the
change first and keeps it only when the function's score gets no worse and no other part's differing rows grow (the
compiler allocates registers over the whole function); the baseline is remembered per definition and project
generation. A compile of the whole definition (compile_and_diff, compile_patch, autopilot) that changes the text or
markers of a part another agent holds is refused before the file is written. `release_part(function, notes)` gives
the part back with a note.

`big_function_report()` counts the functions of 400 instructions or more: matched, open, tried, compiled at least
once, the closest open ones (source-tree projects), compiles on them in the last 7 days (and with compile_part)
against the 7 before, and parts held now. Briefs of big functions in every mode name the calls that repeat four
times or more, list the `#define` lines of the function's file (ROM projects: of matched code in the same unit and
the look-alike), and show a matched look-alike's or twin's C up to 32 KB. Parts are not available in ROM projects
(ROADMAP.md).


## Code blocks in large functions

`find_code_blocks(function="work", limit=5)` is available in both project and ROM modes. It searches target
assembly for straight-line fragments also present in other functions, favoring recorded matched donors in its
shortlist. Results give target and donor instruction offsets, length, and provenance. A small saved matched donor
definition may be included; it is the whole definition, not a source-to-assembly mapping.

The search consistently renames registers while preserving widths, alias relationships, constants, stack offsets
and referenced symbols. Fragments contain 8–96 instructions and cannot cross recognized control transfers or
incoming branch targets. Common prologues and simple move runs are filtered out. These are advisory similarities:
interleaved helpers, reordered instructions, changed stack layouts and multi-block helpers can be missed. A donor's
recorded match is not reverified by this tool. Any reused C must pass the normal strict judge.

Unmatched functions with at least 200 instructions receive up to three strongest non-overlapping findings in their
normal and compact briefs. Autopilot warms findings for up to sixteen large open functions, including when there
are no drafts. Analysis failures do not prevent claims or compilation.

Work is bounded: the explicit tool checks up to 24 shortlisted donors, automatic briefs up to eight, and comparison
examines at most 12,000 donor windows and the first 12,000 instructions of each function. Coverage and truncation
are reported when findings are returned. Missing project mnemonic entries are warmed for up to 24 units per
explicit request or four automatically; further background indexing can reveal additional donors. Target-object
caches are invalidated by file metadata and bounded by unit and instruction counts.

The finder reads targets and saved donor source. It does not compile, edit source, record attempts, change claims,
or accept/update matches. Tests enforce read-only database access and unchanged source/target files. Run
`pytest tests/test_code_blocks.py`; the optional real-object test uses the same `DECOMP_TEST_CLANG` setting above.


## Matched-function pattern library

`function_patterns(function="work", limit=5)` retrieves up to eight recorded matched donors from the same project.
It ranks target mnemonic sequences using weighted opcode, pair and triple counts, with a length penalty. Saved C
never influences retrieval. This is useful for finding function families even when the strict block finder finds
no identical fragment. Architecture-specific labels describe observable operations, not inferred constructors or
semantic equivalence.

The result cites each donor and unit, then reports explicit source spellings (target attributes, vector intrinsics,
unsigned types, loops, switches, volatile, memcpy and signal fences). Support and absence counts refer only to the
retrieved readable definitions, grouped by unit. A single donor remains a single example; even repeated observations
are not compiler rules. Different units may use different settings. The tool does not infer struct types, original
source, or causality from matching code. Use `guess_struct`, donor C and `find_code_blocks` for further evidence.

Normal and compact briefs add at most two donors above the stricter 0.65 retrieval threshold for unmatched functions
of at least eight instructions. Explicit queries use 0.45. These scores are heuristics, not probabilities. Autopilot
warms these hints alongside the block finder. No hint changes compiler flags, sources, claims, attempts or matches.
The existing strict judge remains the only acceptance path.

The library uses the existing target mnemonic index in ROM and project modes. It excludes missing/stale project
entries and reports coverage. It is capped at 5,000 donors, 12,000 instructions per donor and 250,000 instructions
total. Briefs do not trigger a project-wide disassembly. The immutable retrieval cache refreshes when available
matched donor identities or instruction sequences change; selected donor sources and match status are read fresh.
Definitions exceeding 64 KiB are not mined. Advisory failures never block normal work.

`pattern_index_report(sample=10)` tests up to 40 deterministic held-out queries. Each query is removed before fitting
its retrieval index; identical opcode/source clones are excluded from returned donors. It reports top-three overlap
with observed source traits and a deterministic random-donor baseline. This is a weak retrieval diagnostic: generic
traits can agree by chance. It does not measure semantic family accuracy, matches gained, time saved or token savings.
Only definitions with recognized traits are eligible for queries; retained source text is capped at 2 MB. Evaluation
is explicit, never added to each brief. Run `pytest tests/test_function_patterns.py` for synthetic held-out families,
clone exclusions, abstention, invalidation and read-only database/file checks.


## Tiny-function solver

`solve_tiny_functions(limit=100, apply=false)` audits up to 100 open, unclaimed functions whose recorded size is
1–3 instructions. It reads actual target instructions before choosing a template; metadata counts can be stale.
`apply=true` previews project candidates with restoration, then submits only exact winners through the ordinary
judge. ROM mode uses the existing compile/link verdict and records only strict matches. Claims are reserved and
released transactionally. Matched functions, active claims and room-member bulk work are skipped.

Autopilot runs this stage before drafts, even when no draft decompiler is available. Its saved cursor rotates the
bounded sample across runs so early unsupported functions do not starve later ones. Each function gets at most one
candidate per pass. No failed experiment becomes a negative rule for other units or compiler settings.

Current templates: empty void returns on x86, PowerPC, ARM/Thumb and MIPS (including its nop delay slot); x86 integer
constant returns, ud2 traps and direct tail jumps with explicit compatible signatures. Existing simple C signatures
must be present in the unit (ROM: context or saved source). No return types, arguments, complex C++ declarations,
varargs, indirect-jump targets or padding boundaries are guessed. Missing/complex signatures and unsupported shapes
are counted separately. This conservatism means many one-instruction jumps still need ABI recovery. Function-level
compiler attributes and broader architecture templates remain future work.

One x86-64 jump needs no signature: `jmp qword ptr [slot]`, a tail call through the function pointer at a symbol
with nothing added (an ELF program's PLT entry jumping through its GOT slot). It forwards every register and the stack
untouched, so every signature compiles to the same instruction. With no declaration of the function in its file the
template writes `void f(void) { slot[0](); }`; with a simple one it passes its arguments on through a cast. A slot
the file doesn't declare is declared `extern void (*slot[])(void)`, hidden (the target reads it RIP-relative); one
declared as anything else, or a function the file names without a simple declaration, is left to agents. Direct tail
jumps now read objdiff's addend, which it writes as text ("-4"): before, none of them was recognized.


### Scoped traversal and call-preservation observations

The pattern library recognizes explicit acquire atomic loads, acq_rel signal fences, and `weak,noinline` attributes.
When a saved matched definition contains an acquire load, an acq_rel signal fence and `target("arch=btver2")`, its
compact brief retains the three-part observation together. This is co-occurrence evidence: it does not prove that
any component is necessary or sufficient, nor that the original program used atomics. Inspect the donor's exact
placement and loop structure before trying a similar change through the strict judge.

`weak,noinline` is reported as a source spelling with a symbol-binding caveat. It can be a candidate to investigate
when defining a callee changes a caller's relocation, but the index does not infer that it caused a match. In
particular, a matched caller does not make its unmatched callee a matched donor. Changes to shared callees require
validation of the caller and all previously matched neighbors; no attribute is added automatically.


## Agent prompt workflow

Agents start with the best saved attempt, diagnose one concrete mismatch and prefer revision-checked patches for
small changes. Donor hints are reused before requesting more context; library evaluations and bulk tiny solving
are maintenance/autopilot work. Large functions are edited one region at a time but always judged as a whole.
Stall limits and strict matching remain unchanged. Release notes carry tried hypotheses and exact remaining diffs.

A console guide can define `## Agent quick reference`. When present, startup injects that section plus the short
comparison checklist instead of all workflow/objdiff/compiler guides. Full guides remain available through `guide`.
Consoles without a quick reference retain their existing startup guides. Keep quick references current: historical
compiler limitations and obsolete target formats must not override current target evidence.

### Finishing near matches

`finish_near_matches(limit=50, apply=false)` audits a bounded set of source-tree functions at 98–100%.
Autopilot runs it before drafting. The previous report supplies a shortlist; the tool rechecks the current unit,
skips claimed/matched functions, and tries local scheduling only for reordered identical instructions or typed
aliases for unambiguous named references. Only an exact preview may reach the normal judge for application;
matched neighbors, dependencies and claims remain protected. Judged nonmatching trials are cached for the current
revision within the app session. Build failures or unavailable comparisons remain eligible for a later retry. Each pass examines at most 50 functions, with a 120-second budget checked between functions.

Raw-address/target repairs, generated local tables and branch semantics are reported for review, never guessed.
No global compiler flags or target bytes are edited. ROM-mode finishing remains planned. Revisions ignore usage
accounting, suggestions and autopilot progress while continuing to track source, headers, build scripts/settings
and target contents.

### A shared switch path with the wrong block order

When the instructions match but a switch's zero/default blocks appear in the opposite order, inspect the original
branch destinations before changing the C. In a verified shared-label shape, a compiler-only
`__atomic_signal_fence(__ATOMIC_ACQ_REL)` on the default entry, **before** the shared label, can keep that entry
distinct from a case that jumps directly to the label. This can recover the original block placement without
emitting a fence instruction. A fence after the shared label affects both incoming paths and tests a different
hypothesis. Try the change as a scoped `try_recipes` edit and keep it only after strict matching and neighbor checks;
it is not a general switch-rewriting rule.

For raw RIP-relative data operands, verify the original executable destination and mapped segment first. A diff's
section-relative literal is not a game address, and an imported draft's suggested address is not relocation evidence.
Repair missing target metadata from the original bytes before interpreting the remaining source-layout differences.

`arch.x86.rip_data_references` provides read-only evidence for such audits: original displacement-field offsets,
original image destinations, and PC32 addends, including instructions with trailing immediates. Callers supply
verified mapped data ranges. TLS operands are excluded. Before migrating existing metadata, check original bytes,
existing relocation overlap and function ownership; preserve already matched functions, then compare complete
units before and after. The helper does not migrate targets or infer addresses from draft names.

### Scoped loop and vector-load experiments

For a traversal with no calls inside the loop, a compiler-only signal fence at the very start of the loop can
preserve a single shared null-test header when plain C introduces an extra entry test. This has been verified for
one traversal shape; the same fence did not recover another traversal's register allocation. Check the actual
branch layout and retain only a strict winner.

A redundant low-byte/whole-word check may need separate source constraints: a signal fence between the tests,
an acquire load of the word, and a likely low-byte-zero branch recovered one 40-byte record loop. Each component
changed a distinct instruction or join-layout decision. This is an experiment for that observed shape, not a rule
to add atomic loads or branch hints to arbitrary loops.

When an unaligned vector load becomes an aligned instruction, inspect the declaration's alignment guarantee.
A byte-array declaration plus `_mm_loadu_ps` can prevent the compiler from assuming vector alignment; casting
an already aligned vector declaration may preserve that assumption. One reported trial improved the load opcode
and score but did not reach a strict match. Treat it as a scoped preview, preserve symbol visibility and ABI, and
verify all users of a shared declaration.

### TLS instruction shape can be a linker decision

An explicit thread-base load followed by an offset-slot add can be the linked form of **general-dynamic TLS**,
rather than initial-exec TLS with segment folding disabled. The compiler first allocates registers around a TLS
resolver call; an equal-length linker relaxation replaces that call sequence later. This explains why reproducing
only the final loads may lose a callee-saved register or stack frame. In a verified family, compiling the scoped
sources as PIC and applying canonical TLSGD-to-IE relaxation recovered strict matches with the original compiler.

`arch.x86_tls.relax_tls_gd_to_ie` requires an explicit TLS-variable-to-slot mapping and matching FS-base/slot-add
evidence in the same target function. It accepts only the canonical 16-byte resolver pair, retains its length,
replaces its two relocations with the slot's PC32 relocation, and leaves the TLS variable symbol intact. Unknown
mappings, other resolvers, different addends, architectures and instruction forms are rejected. It does not infer
slot identities, alter targets, or select compiler flags. Validate source/visibility effects of PIC and all matched
neighbors before changing any project's per-file settings; the helper does not enable itself automatically.

### Entry frames with a tail-call exit

If the target establishes its frame at entry but Clang sinks the prologue into a call-only branch, test the pinned
compiler's `-mllvm -enable-shrink-wrap=false` in scratch. This controls frame placement without disabling sibling
calls. Declaring a callee `returns_twice` can also move the frame, but can suppress tail calls; that partial result
does not establish the callee's real convention. Remove such an experimental declaration only after checking the
actual callee and every affected caller.

Use a per-unit override only after the whole unit passes its protected-match checks. A verified family recovered
strict entry frames and tail jumps with the original compiler; other functions improved but still needed separate
callee-identity or scheduling corrections. Keep those corrections explicit, retain only exact winners, and leave
unproven units on their existing flags. There is no automatic project-wide shrink-wrapping change.

## Persistent experiment history

`experiment_history(function="work", limit=6, before=0)` works in ROM and source-tree projects through MCP
and the API helper. It reads existing recorded attempts across agents and restarts: attempt ID, agent,
submission path, context version, compile outcome, score, source hash and a bounded source change from the
preceding attempt. The default page has six entries; the maximum is twenty. Use the returned `before` ID
for older pages. Reading history does not compile, claim, edit files, or change scores.

Consult it before repeating a previous experiment. Identical sources and equal scores are observations,
not proof of identical generated assembly. Context versions do not fingerprint the complete build state;
historical verdicts are never reused by this tool. Named recipe previews, including baselines and negative results, are stored separately and shown as recent E IDs.
Their source revision, timing and generated-code fingerprints help detect repeated experiments across agents and
restarts. Attempt pagination does not paginate these recent previews. Fingerprints include code, relocations and
local data; they are observations, never cached correctness verdicts. Complete build-state caching remains planned.

Automatic `easiest` and `finish` selection now prefers fewer historical attempts among otherwise comparable
functions. Size bands, dependencies, release cooldowns, twins and file-completion priorities still apply.
This is a soft tie-breaker: retried functions remain eligible, and named claims can resume them immediately.
The heuristic uses historical attempts, including before build changes; it is not a prediction of match probability.


## Single owner and bounded specialist advice

The claim holder owns source edits, builds and acceptance. `request_specialist` records one concrete blocker
(control_flow, layout, codegen or references) with evidence and the current source revision. Existing helpers can
take that request; it does not launch more agents. Specialists receive bounded read access and can report findings
and explicit recipe proposals, but cannot claim, edit, compile or apply them. The owner reviews proposals against
shared experiment history and uses the ordinary judge. Requests expire after ten minutes; lost claims cancel them,
and changed source revisions make reports stale. Specialist sessions retain their read-only role until cleanup.

`inspect_blocks` selects CFG blocks by target offset or page, showing predecessors, successors, register and flag
use/definitions, conservative stack effects, and localized differences from a current target-compatible diff.
Unknown edges, calls and partial register effects remain explicit. ELF x86 targets support data flow; unsupported
formats or architectures abstain rather than inventing it.

`suggest_recipes` reuses existing donor retrieval for observations and proposes compiler experiments only for
recognized scheduling or integer-copy mismatches. `try_recipes` rechecks evidence against a fresh baseline, skips
duplicate source within a batch, and records results. Explicit bounded patches remain available for other hypotheses.

Supported x86-64 ELF evidence is independently checked against branch/data destinations, instruction and function
boundaries, relocation operands and unmasked bytes before judging. Once declared, supported original evidence
remains required across restarts. Other formats explicitly report unavailable; projects without supported declarations
retain their existing strict judge. Target changes during a comparison invalidate it. Each source trial rebuilds a
whole-unit baseline and protects matched neighbors; this adds build cost rather than reusing stale verdicts.

## Durable compile operations and region experiments

`compile_start(function, request_id, revision, action="patch", edits=[...])` reserves a durable request identity
and returns an operation ID immediately. Use `compile_status(operation_id)` until terminal. Status waits up to five
seconds for a state change by default (`wait_ms=0` returns immediately; maximum 10000). A transport timeout does
not cancel the operation. Retry with the **same request_id and identical arguments**: no second mutation runs.
Changing arguments under an existing key is rejected. New keys cannot queue a second operation for the same
function or owner while one is active. There are at most eight active/pending operations and two background workers.
Independent source units can run concurrently; jobs sharing a source file stay serialized without occupying the
second worker while waiting. Existing tree locks still protect shared edits. Request identity uses caller arguments
and the stable function ID, excluding diagnostic source snapshots; older saved requests follow the same rule.

Actions are `patch` (revision and exact edits), `definition` (complete source; empty revision only when its definition
is absent), `recipes` (the usual recipes/apply inputs), and `diagnostics`. Every source-changing action calls the
existing compile/preview implementation and strict judge. All existing match, constant, relocation and neighbor
checks remain mandatory. Mutation tools refuse intervening edits/releases through the tool interface while an
operation is active. External editors still require coordination. Claims are checked at submission and execution. A heartbeat renews existing, unexpired claims belonging to queued
or running operations in this process. Released, expired or reassigned claims are never recreated.

States are `queued`, `validating`, `building_and_judging`, `completed`, `failed`, and `interrupted`. Completed means
the operation returned; its text still says whether compilation failed, a proposal was rejected, or a function
matched. Queue/build/validation/diff work is currently timed together; the state does not claim per-phase timing.
Results are private to the submitting agent/project. Old results describe old builds; they are never accepted as
fresh verdicts. On process restart, unfinished operations become interrupted and are **never replayed**. Their
source edits may have landed; inspect current source and history before choosing a new request ID. This is not a
cross-process transaction or crash rollback facility. Poll to completion before ending a helper session.

For a larger function:

1. Read `region_history` and `experiment_history`, then inspect target blocks.
2. Use `record_region(function, revision, start, end, hypothesis, evidence)` for one concrete hypothesis. Offsets are
   inclusive target **instruction** offsets, not source lines. Negative/recovered findings can be recorded with an
   explicit outcome. No record approves a partial match.
3. Pass its returned `region_id` to `compile_start`. For a patch/definition, the worker obtains a fresh whole-function
   baseline in preview mode under the source lock before the edit. This extra protection costs another build.
4. Poll status, then read `compare_region`. It reports changes in differing-row counts for target blocks inside and
   outside the region. Insertions map to adjacent target instructions; counts are not semantic proofs or regional
   match percentages. Changed/misaligned target rows make the comparison unavailable instead of guessing.

History, evidence, source revision, linked operation, findings and comparisons survive helpers/restarts. Concise
regional findings also appear in `experiment_history`, so a new helper sees earlier hypotheses and failed ideas.
Giant-function partial recovery can be recorded as `recovered` even when the whole-function score stays flat.

### Isolated compiler explanations

`compile_start(..., action="diagnostics")` uses a console adapter for the project's configured compiler. Initially
supported: generated PS4 Clang projects with a recognized `tools/compile.py` Ninja command. Other compilers/projects
explicitly report unavailable. The adapter reads the command with Ninja's nonbuilding `-t commands`; it never runs
project build scripts. Function/unit flags are retained, including known per-unit flags.

The tool copies the selected unit and project headers (at most16 MiB) to a temporary directory, rejects symbolic
links, then runs the configured Clang with diagnostic-only LLVM IR/debug-line and optimization-remark flags. Runs
are memory-capped where the platform supports it and time-limited to45 seconds. It returns bounded optimized IR
for the selected function and loop/vectorization remarks; neighboring definitions may appear in remarks. Missing
remarks do not prove an optimization did not run. Unknown wrappers abstain; a failed diagnostic compile never
changes the ordinary build. A revision change during the run labels results as older copied inputs.

### Scoped recipe proposals

`save_recipe(name, prerequisites, evidence, donors, edits)` stores explicit prerequisites and1–8 currently recorded
matched donor names. `recipe_candidates(function)` returns proposals only when the captured project build scope,
donor target hashes/status and exact edit strings still fit. Build scope includes project build/objdiff settings
and the compile/per-unit configuration files; it is not a complete identity of external compiler dependencies.
Recorded donor status is **not freshly reverified** by retrieval. No semantic equivalence, transfer success or
causality is inferred. Owners check prerequisites, then test a proposed application through `try_recipes` or a
queued recipes operation. That performs fresh strict judging and ordinary neighbor protection.

### Boundary checks on supplied integer models

`check_integer_behavior(function, region_id, target_model, candidate_model, cases)` records concrete boundary
counterexamples with a regional experiment. This initial implementation evaluates **supplied models**, not C or
the original executable. It cannot automatically detect whether the models accurately represent either program.
A pass is never a behavior-equivalence or matching verdict.

Models use prefix arrays and explicit integer casts. For example `["u16",["add","n",1]]` wraps an increment to16
bits. Supported operations: `add`, `sub`, `mul`, `and`, `or`, `xor`, `eq`, `lt`, `le`, and `select(condition,yes,no)`;
casts are `u8/u16/u32/u64` and `i8/i16/i32/i64`. Arithmetic is mathematical integer arithmetic until an explicit
cast. There is no Python/C eval, arbitrary code execution, shift/division ambiguity, memory model, or float model.
Provide1–1024 input dictionaries. Results include mismatch count and up to eight counterexamples and retain the
models; this is useful for refcount overflow, signedness and copy-extent hypotheses. Runtime differential execution
of recovered functions with relocation/callee models remains future work.

### Target digest reuse for donor hints

Block and pattern lookup share a bounded cache of target digests. Each lookup checks file identity, size and change
times; recently changed objects are reread to avoid timestamp races. Entries expire after 30 seconds even when
metadata stays unchanged, and objects changed during a read are excluded from that lookup. Decoded block caches
also track the target digest. This cache is advisory only: it never supplies a matching verdict to the judge.
