# Matching PlayStation 4 code (x86-64, clang 5.0.2)


## Agent quick reference

- Judge current targets with strict relocation and constant checks. Historical scores from earlier target formats
  are not comparable. Keep repaired function-local symbol-linkage aliases intact.
- Start from the best saved source and inspect the actual prologue, signature, destinations and failing region.
  Database instruction counts may be estimates. A skeleton or guessed signature is not ABI evidence.
- Use `function_brief` and `compile_patch` for small function edits. Use `edit_file` for shared declarations/helpers;
  preserve all matched neighbors. Target objects, compiler configuration and build scripts are not agent-editable.
- For globals, a relaxed address load followed by a separate access can need default visibility; a folded direct
  RIP-relative access can need hidden visibility. The current binder supports eligible GOT relaxation. Inspect the
  target and relocation rather than applying visibility rules globally. See the data-reference section for details.
- If only independent instruction ordering remains after checking operands and semantics, test local
  `target("arch=btver2")` before releasing. Recheck the whole function. Scheduling failure alone is not proof that
  the compiler cannot match it. Send only genuine remaining scheduling/register differences to the permuter.
- Keep `no-sse` experiments local to compatible integer-only by-value callers. Do not enable AVX2/BMI2, global
  fast-math or another architecture just to force one expression. The configured build already supplies its ISA.
- A target canary requires reconstructing the observed protected locals. The current patched compiler/binder
  supports the global guard; old TLS-only/global-guard blocker notes are not current limitations.
- A unit can be built with another compiler (clang 3.8.1 or 3.9.1) where its functions show it: if C that looks
  right stalls on register choice or instruction order, try_compiler(function, "all"). One match is kept as evidence;
  the unit switches once a second of its functions matches only with that compiler and none of its matches is lost.
  Each unit's settings and why are in docs/build-settings.md (the brief quotes its file's).
- A switch's jump table is bound to the game's by the build (tools/jump_tables.py) when every case lands where the
  game's does: a remaining `lea reg,[D_...]` against `[.rodata...]` row means some case's code or order differs, not
  a tool limit. Fix the cases; don't release the function for it.
- A function of 400+ instructions goes a part at a time: `list_parts` shows its parts, mark them in the C with
  `/* part N */ ... /* end part N */` and send one part's C with `compile_part`; `claim_part` joins someone else's.
  Repeated calls with other constants become one-letter macros in the file (`edit_file`).
- Recover vector helpers from the target operations, masks and order; original helper source is not required.
  Use the donor and block hints in the brief. Read detailed PS4 recipes only when the current diff calls for them.
- Acquire loads, fences and scheduling attributes are scoped experiments. When reusing a saved traversal example
  containing all three, retain the complete observed combination until isolation tests establish necessity.
  `weak,noinline` changes symbol binding; validate the caller and matched neighbors, not just the callee.

## Validate targets before tuning C

Target format 2 fixes ELF symbol indices, short-branch operand widths, and function extents.
Use `functionRelocDiffs: all`: a matching instruction with the wrong call or data destination is not a match.
Old scores and old target disassembly must be rechecked after migration; do not use them as evidence that C is correct.
Recovered sources can contain function-local `extern __typeof__(...)` declarations with an `__asm__("symbol")`
linkage name. These preserve the existing type while correcting that function's destination. They contain no assembly
instructions. Keep the linkage names intact; do not globally rename an identifier based on one repaired call site.


The compiler this project builds with was found by measurement, not from the SDK's name. On inFAMOUS Second Son's
eboot (`ET_SCE_EXEC`, ELF64, no section headers), clang 5.0.2 with

    -O2 -mavx -fno-omit-frame-pointer -fPIE -mllvm -x86-use-vzeroupper=0

reproduced 14 of 20 hand-transcribed functions byte for byte, and 18 of 20 as the same instruction set. Each flag is
there because a whole population of the game's functions says so:

- **`-fno-omit-frame-pointer`**: 99.7% of the 22,815 functions that make a call open `push rbp; mov rbp,rsp`. Without
  it, every function with a call misses.
- **`-mllvm -x86-use-vzeroupper=0`**: 21,093 of 27,602 functions use VEX/AVX encodings, and exactly one contains
  `vzeroupper`. Stock clang puts one at every exit that uses a YMM register.
- **`-mavx`**: the zero idiom is `c5 fc 57 c0` (`vxorps ymm0,ymm0,ymm0`, VEX.256). clang 6 and newer use the 128-bit
  form and miss it.
- **`-fPIE`**: the game addresses globals with `lea rax,[rip+disp]`, as a position-independent executable does. Without
  it clang writes `mov eax,imm32` and every function that touches a global misses.
- **`-mllvm -unroll-runtime=false`**: the game does not runtime-unroll its loops. `func_00508220`'s target is a plain
  `mov / cmp / cmovge / add / dec / jne` loop; clang 5.0.2 at `-O2` emits an `and $3` / `cmp $3` / `jae` preamble,
  four copies of the body and a `neg` tail instead. `-fno-unroll-loops` compiles identically here but also turns off
  compile-time unrolling of constant-trip loops, which nothing says the game lacks, so the narrow flag is in. A/B on
  a frozen snapshot of all 2,567 units: 302 matched either way, `func_00508220` 0 -> 58.33%. It does not match those
  loops by itself: the target also duplicates the `-1` initialisation and pads the loop with 16-byte `nopw`.

## The stack protector, and the compiler this project needs

**The guard pointer is bound at build time.** The game loads it from one GOT slot at a fixed address, `0xd9de00`
(210 of 210 sampled canary functions), while clang writes a `GOTPCREL` relocation for a linker that never runs. A
relocation on one side of the comparison against a literal on the other never matches, so `tools/bind.py` — run by
`tools/compile.py` after every compile — writes the game's own displacement into the object and drops that
relocation. Measured on a frozen snapshot of the whole project: 109 functions improved, typically +2.5% (one row),
none crossed 100% on its own because everything else in them still differs. A function with a canary can now reach
100%; it needs the rest of its code to be exact first.

24.2% of the functions (6,674) call `__stack_chk_fail`, and they are **35.5% of the game's code**. Their prologues load
the guard through the GOT — `mov r12,[rip+disp]; mov rax,[r12]; mov [rbp-0x30],rax` — and every exit reloads it,
`cmp`s, and jumps to `__stack_chk_fail` on mismatch. So the game was built with `-fstack-protector-strong` and the
**global** guard model.

Stock clang 5.0.2 cannot express that on a Linux triple: it puts the guard in the TLS slot (`mov %fs:0x28,%rax`), and
`-mstack-protector-guard=global` only exists from clang ~11 on. The build therefore uses a patched 5.0.2 —
`/opt/llvm-5.0.2-globalguard/bin/clang` — which is stock 5.0.2 with `hasStackGuardSlotTLS()` returning false for Linux,
so the guard becomes the global `__stack_chk_guard` (GOT-loaded under -fPIE) while everything else about the codegen is
unchanged. Verified: the 21-byte zero-idiom function still compiles byte-identically, and a stack-buffer function gets
the game's guard shape with no `%fs:` access.

Practical consequence for you: a function with a local buffer or a large frame **must** produce that canary, and if your
C has no such buffer the target's canary has nothing to pair with — check the target's prologue first, and give the
function the buffer the target shows (an array local, a memcpy destination) so the compiler inserts the canary.

## Spend turns where they pay

- **For a near match, classify the remaining differences before spending more attempts.** Confirm call/data destinations,
  types, and control flow first. If only scheduling or register choice remains, test applicable local recipes such as btver2 before leaving the remainder to the background permuter.
  A high percentage alone does not establish that the C is correct.
- **Write the smallest edit that could be right.** `function_brief` followed by revision-checked
  `compile_patch` avoids resending the whole definition. Reserve `edit_file` for shared declarations and helpers.
- **Read the target's first ten instructions before writing anything.** They say whether the function has a stack
  canary, whether it realigns the stack, which argument registers it keeps, and whether it is a thunk — most wrong
  first attempts are wrong about one of those, and the brief already carries the disassembly.

## Choose work from verified evidence

A corrected-target snapshot had 1,282 matches: 1,195 had at most 25 decoded instructions,
but one had 69. Calls, branches and vector instructions all occur in verified matches;
their presence alone does not imply a compiler limitation. App instruction counts based on
size/4 are estimates; use actual decoding for architecture-specific claims.

Prefer verified matching function families and compatible declarations. A file with no
matches is not evidence of bad flags: grouping files by whether they have matches makes
that conclusion circular. Large functions need block-by-block reconstruction because most
unmatched bytes live there (list_parts and compile_part do it a part at a time); do not
mark them impossible after a few skeleton attempts.

Target format 3 relocates RIP-relative references into BSS (mapped memory without file
bytes). Earlier target literals can make correct data references appear as near-misses.
Recheck current targets before trying scheduling changes; retain strict relocation identities.

## What 5,311 matched functions actually use (counted, not guessed)

Census over every 100% function in the project, by what its C contains:

    __asm__("func_X") externs        1,398  (43%)   the ABI-repair linkage trampoline
    casted pointer arithmetic        1,428  (45%)
    _mm_loadu_ps / _mm_storeu_ps     610 / 576     16-byte copies go through intrinsics, not memcpy
    __builtin_annotation                463  (9%)   an optimisation barrier, see below
    __atomic_signal_fence                190  (6%)
    volatile                             160  (5%)
    __atomic_load_n                       42  (1%)
    __builtin_expect                      28
    __builtin_islessgreater                6
    __builtin_{u,s}sub_overflow            3

Two of those are worth reaching for before anything more elaborate.

**`__builtin_annotation` holds an add/sub pair that clang would cancel.** When the target keeps
`add rdi, 8; ...; add rdi, -8` around a refcount decrement, writing the arithmetic plainly lets clang
cancel it. Annotating the value keeps it:

    if (q && --*(int *)(q + 8) == 0)
        func_000AA250((char *)__builtin_annotation((unsigned long)(q + 8), "refcount") - 8);

That is `func_00065ED0` (matched) and the same line appears in `func_00069340`. The label is arbitrary;
what matters is that the value flows through the annotation, which the optimiser will not fold.

**16-byte copies are written with `_mm_loadu_ps`/`_mm_storeu_ps`.** 610 matched bodies use the load and
576 the store; `__builtin_memcpy(a, b, 16)` produces `vmovups` from the same clang and does not compare
to the game's `movq/movq` or `vmovups` pairs depending on the unit.

**A symbol referenced by an asm name is how 43% of matches link.** The pattern the project uses:

    extern __typeof__(func_00984350) repaired_func_000000A0_1 __asm__("func_00984370")
        __attribute__((visibility("hidden")));

It names the callee `func_00984370` (so the call relocates against the real target symbol) while the
declaration keeps the ABI types the recovered call needed. Renaming such a declaration's symbol is what
makes a match fragile - see the naming note in the tool section.

## An unsigned bounds check whose comparison order the target keeps

`cmp (%rsi),%edi; jae` (index first, bound in memory) and `cmp %edi,(%rsi); jbe` (bound first) are the same test
and different bytes. Clang writes the second for `if (i < s->count)`; the target has the first. Writing the test as
an overflow check on the subtraction restores the order:

    unsigned unused;
    if (__builtin_usub_overflow(i, s->count, &unused)) return 0;
    return s->items[(unsigned short)i];

Verified by compiling both against the same struct: plain gives `cmp %edi,(%rsi); jbe`, the builtin gives
`cmp (%rsi),%edi; jae` with no `sub` emitted (the difference is dead, only its flags are used). On this project it
took `func_007DB030` from 90.5% to 100% (matched, score 0). It only matters when one side of the test is a memory
operand - with the bound in a register clang already emits the index-first order.

## Canonical relaxed TLSGD access uses -fPIC: name the slot in the C

The game's TLS access is `mov rax,fs:[0x0]; add rax,[D_slot]; mov rax,[rax]` - a thread pointer plus a module offset
word, three instructions. clang 5 emits two (`mov rcx,[D]; mov fs:[rcx],rcx`, R_X86_64_GOTTPOFF) unless the unit is
compiled for shared objects, where it emits the TLSGD resolver pair that `tools/tls_relax.py` (called from `bind.py`)
rewrites into the game's form.

1. **The build does the -fPIC part.** decompdecomp gives every unit whose target has the sequence `-fPIC` (in
   `tools/compiler_units.json`, with the functions that show it; `tools/compile.py` drops the base `-fPIE` for it),
   kept only where no match of the unit is lost. `docs/build-settings.md` lists them. A unit that stayed `-fPIE`
   because one of its matches needs it: split the TLS function into a file of its own (split_file), and it gets
   `-fPIC` at the next Check again. Don't ask for the flag: it's set from the target's bytes.
2. **The source names the slot by its name in `tools/tls_slots.json`, not the raw `D_` symbol.** The relaxation pairs
   the TLSGD sequence with the target's slot only through that file. `D_00D9E060` is `decomp_tls_context`; every
   other slot is `decomp_tls_` + its symbol (`D_00D9E080` is `decomp_tls_D_00D9E080`):
   `extern __thread void *decomp_tls_D_00D9E080 __attribute__((tls_model("global-dynamic")));`. A raw
   `extern __thread void *D_00D9E080` never matches a slot and stays GOTTPOFF.

With both, `func_0033A950` in `text_00339D40` went 39.3% -> **100%**. `__builtin_thread_pointer()` doesn't compile
with clang 5 (the backend can't select it). Ordinary `address_space(257)` loads can fold into `fs:[reg]`.

Do not confuse that canonical sequence with `mov offset,[D_slot]; mov base,fs:[0]; mov value,[base+offset]`.
The latter is not the 16-byte TLSGD linker relaxation, and does not make the automatic detector choose -fPIC.
For that explicit-base form, an acquire `__atomic_load_n` of an unsigned-long pointer in address_space(257)
can preserve the base register. Read the ordinary offset word where the original does; delaying it past object
stores changes scheduling. The complete matched donor `func_002F2630` in `text_002F1FF0` combines the early
offset read and atomic base with its growth-loop recipe. Retrieve that source; do not generalize it to all TLS.

PS4 `split_file` now divides a source at a function in the function-map config and inherits its compiler/flags.
The engine rebuilds both files and rejects splits that lose an exact neighbor. To isolate one function in the
middle, split at it, then split its successor into a third file; `try_compiler` can test the one-function unit.
Original function addresses, extents and relocations stay unchanged.

## Data references: read the target before choosing visibility

The game's code was linked, so a data reference appears in it in one of two shapes, and which one decides the
declaration. Getting this backwards costs the match.

- **Two instructions** - `lea rax,[rip+D]` then a load or store through `rax` - is a default-visibility access the
  linker relaxed (GOTPCRELX). Declare the symbol plainly (`extern char D_xxx[];`): clang then emits the pre-link
  `mov rax,[rip+GOT]`, and `tools/bind.py` performs the same relaxation in the object, which is what makes the row
  match. Measured on `func_000EB270`: plain declaration 80%, hidden 46.7%, plain + relaxation **100%**.
- **One instruction** - the offset folded in (`vmovss 0xc0(%rip),%xmm0`, `cmp byte ptr [rip+D], 0`) - is a
  non-preemptible access: declare the symbol `__attribute__((visibility("hidden")))` so clang folds it the same way.
- Never write `hidden` on a symbol whose target loads in two instructions: it folds the address away and no amount
  of C gets it back.

## Spellings that turned near misses into matches

Each of these was measured on a real function here: the first spelling is what the game's build did, the second is
what clang 5.0.2 does without it.

- **Call or jump to another function**: declare the callee `__attribute__((visibility("hidden")))`. A
  default-visibility declaration goes through `PLT32` and the judge shows `call <next instruction>` instead of the
  symbol; hidden makes it a direct `PC32` reference. Turned `func_0078AD80` 87.1% -> 100%, `func_0016B5C0` and
  `func_0016B5F0` 95-96% -> 100%, `func_0034FD80`'s four call/jump rows -> 100%. The same attribute on **data**
  can be worse: it lets clang fold a test into one `cmp qword ptr [rip+sym], 0` and lose an instruction, so choose data visibility from the actual one-instruction or two-instruction shape described above. Code
  visibility also needs verification against the actual call relocation; do not rewrite declarations globally.
- **`fcmp ONE`** (the game's `a != b` on floats): write `__builtin_islessgreater(a, b)`. `a != b` is `fcmp UNE`,
  which clang expands to `setp` + `or`; the builtin is the single `vucomiss; setne` the game has.
- **A byte the game keeps in a register** (`mov al,[x]; test al,al`): read it with
  `__atomic_load_n(&x, __ATOMIC_ACQUIRE)`. A plain or `volatile` read is folded into `cmp byte ptr [x], 0`.
- **A redundant `add rdi,8; add rdi,-8` pair the game keeps before a call**: cast the pointer to `unsigned long`,
  `__atomic_signal_fence(__ATOMIC_ACQ_REL)`, then call `(void *)(ref - 8)`. Pointer-only arithmetic cancels both
  adds even with the fence.
- **A shared exit several failure branches jump to**: write one result variable and `goto`s
  (`u8 r = 0; ... goto out; zero: r = 0; out: return r;`). An `if/else` gets if-converted to `setne` and the value
  rematerialised into `eax`.
- **A `? :` select whose result is then dereferenced** (`lea rax,[rsi+8]; test rsi,rsi; cmove rax,rsi;
  mov ecx,[rax]`): put the select in a **`volatile`** pointer — `volatile int *at = q ? (int *)(q + 8) : 0;` then
  read `*at`. With a plain `int *at` clang derives `q != NULL` from the dereference and drops the select entirely
  (`mov ecx,[rsi+8]`), and a `q ? *at : 0` form turns it into a branch. Measured on `func_00381A10`
  (77.6% -> 92.0%) and it keeps the select in `func_00381980`'s twin block; the load itself stays a plain `mov`.
- **A loop with a call and a count reloaded after it**: hoist the count (`signed long n = *(int *)(self + off);`)
  and write `do { ...; ++i; } while (i < n);`. In `for (...; i < *(int *)(...); i++)` clang schedules `inc` first
  and the `movsxd` lands on the wrong side.
- **Intra-unit calls**: retain a separate, non-inlined callee. The binder normalizes a defined function's PLT32
  call or tail jump to PC32 only when the original owner branches to that same symbol with the same opcode.
  It does not fix an incorrect destination, external call or section-relative branch. Inspect the actual relocation
  before trying more `weak`/`noinline` declarations.

## Keep the build able to run

- **Scratch files go outside `src/`.** `build.ninja` lists every file under `src/` as an input of the rule that
  regenerates it, so a scratch `.c` that is created and then deleted leaves ninja unable to rebuild anything
  ("needed by 'build.ninja', missing and no known rule to make it") until `tools/configure.py` runs again.
- **A source that does not compile stops every unit.** One bad cast in one file makes ninja stop at that unit and the
  project measure as if the rest did not exist. Build the unit you changed (`ninja build/<version>/src/auto/<unit>.o`)
  before moving on.

## Investigate remaining differences before changing the compiler

Store merging, instruction scheduling and branch shapes are hypotheses to isolate, not
proof of an SDK compiler difference. First check types, widths, alignment, aliasing,
control flow and symbol destinations. Identical instruction multisets do not establish
identical semantics. Only send genuine scheduling/register differences to the permuter.

Earlier scheduling experiments failed to reproduce some independent instruction swaps. They did not establish
that all local compiler settings fail: later tests of `target("arch=btver2")` matched several such cases. After
checking control flow, widths, constants and destinations, try that local scheduling model through the normal
judge before releasing. A result from one function is not a guarantee for another. Record the exact remaining
rows and experiments; do not use inline assembly as a scheduling workaround.

The pinned compiler already enables SSE4.1 under `-mavx`; `_mm_max_epu32` emits `vpmaxud`.
Do not request extra ISA flags solely because scalar C produced a scalar max.

For large structs passed by value on the stack, local `__attribute__((target("no-sse")))`
can replace overlapping YMM copies with qword copies in integer-only callers. Tested
56-byte cases improved from about 42-46% to 78-80%, but grouping/registers still differed.
The attribute affects the whole function. Keep it local and check the function's complete
instruction and ABI requirements. A 24+32-byte grouping remains a hypothesis, not recovered
source or a reason to change global flags.

For vector math, reconstruct small always-inline intrinsic helpers rather than requiring
original helper source. `vdpps` immediate 0x7f means XYZ dot product replicated to all lanes;
`_mm_dp_ps(a,b,0x7f)` expresses it. `_mm_rsqrt_ps` followed by `_mm_mul_ps` expresses the
observed approximate normalization sequence; substituting scalar sqrt/division changes it.
Reconstruct zero-length handling, masks, shuffle immediates and operation order from the
assembly. Do not enable global fast-math to force a local sequence.

Read original branch destinations, not nearby function names or unit-relative disassembly
addresses. An old analysis of `func_0070BD40` called the adjacent `func_0070BE90` a dependency,
but the original direct call is to `func_00771C80`. The vector function itself has no FS
access. Treat the wrapper's TLS folding problem separately and recover the vector function's
actual incoming arguments before replacing its skeleton. A placeholder signature is not ABI evidence.

## What the code looks like

- Functions are `func_<address>` (`func_00000320`), data `D_<address>` (`D_9866D0`); write them with those names in C
  and the build gives them the original's symbols.
- The eboot is linked at 0 and loaded anywhere, so absolute addresses in the code are image offsets.
- A function's symbol size can include trailing `nop` padding; the delinker keeps the game's bytes, so a match is
  byte-for-byte against what the linker laid out.
- Imports are Sony's NIDs, resolved through a stub table; a call to one shows as a relocation to `func_<stub address>`
  until the NID is named.

## Call matching and target layout

Targets use one text section per function, matching `-ffunction-sections`.
Original addresses are stored separately for guard binding and read from the
project config for the app map. PLT32 relocations normalize to PC32 while keeping
the callee, offset, addend and instruction bytes. Incorrect identities still fail.
Existing projects retain their own tools until explicitly refreshed; refreshing
needs a rebuild and verification that all existing matches remain matched.
