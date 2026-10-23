# Matching PlayStation 4 code (x86-64, clang 5.0.2)

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

- **A function already at 94% or more is the permuter's, not yours.** Its C is right and its last few instructions are
  scheduling or register choice; four to ten more attempts from you usually buy nothing. Take a function that is far
  from matching and leave the near miss alone — the background permuter grinds those on spare cores, without tokens.
- **Write the smallest edit that could be right.** `edit_file` on the one expression that differs costs a fraction of
  the tokens of resubmitting the whole function, and the judge only cares about the result.
- **Read the target's first ten instructions before writing anything.** They say whether the function has a stack
  canary, whether it realigns the stack, which argument registers it keeps, and whether it is a thunk — most wrong
  first attempts are wrong about one of those, and the brief already carries the disassembly.

## Where the matches actually come from (measured on this project)

815 functions matched so far, and they are overwhelmingly small:

| instructions | functions | matched | attempted | matched / attempted |
|---|---|---|---|---|
| 1-10 | 5,081 | 529 | 839 | **63.1%** |
| 11-25 | 5,809 | 210 | 1,199 | 17.5% |
| 26-60 | 7,777 | 75 | 1,089 | 6.9% |
| 61-150 | 5,859 | 0 | 3 | 0% |
| 151+ | 3,076 | 0 | 2 | 0% |

- **Nothing over 60 instructions has ever matched**, and no matched function is bigger than 60; the median matched
  function is 7 instructions and 280 of them are 5 or fewer. Yield falls by an order of magnitude per band. A big
  function is not "more work", it is a different problem - take small ones.
- **There are still 4,121 functions of 10 instructions or fewer that nobody has touched**, and 4,610 of 11-25. At
  the rates above that is where the next two thousand matches are; the code mass is elsewhere (61+ instructions are
  80% of the unmatched bytes and have never converted).
- **Matches cluster by unit**: all 815 sit in 447 units; the other 2,120 units have 1,197 attempts and zero matches.
  Even at 1-10 instructions, 679 attempts in units that already had a match converted, while 161 attempts in units
  without one converted none. (Partly size - median attempted in a cold unit is 24 instructions vs 16 - but not
  enough to explain 0 of 161.) Work a unit that already has a match before opening a new one; if a unit has no match
  after a few tries, its file or its data names are probably wrong, and that is worth a note rather than more
  attempts.
- **A plain `extern` byte global gives a GOT load** (`mov rax,[rip+GOT]; mov (%rax),%al`); the same declaration with
  `__attribute__((visibility("hidden")))` gives one direct `mov al,[rip+D]`. The game also writes `lea` + deref in
  some places - the address stayed live in its C - and neither declaration reproduces that, so don't chase it with
  visibility; it needs the address to escape (a call taking it, a stored pointer), which changes the function's size.

## Spellings that turned near misses into matches

Each of these was measured on a real function here: the first spelling is what the game's build did, the second is
what clang 5.0.2 does without it.

- **Call or jump to another function**: declare the callee `__attribute__((visibility("hidden")))`. A
  default-visibility declaration goes through `PLT32` and the judge shows `call <next instruction>` instead of the
  symbol; hidden makes it a direct `PC32` reference. Turned `func_0078AD80` 87.1% -> 100%, `func_0016B5C0` and
  `func_0016B5F0` 95-96% -> 100%, `func_0034FD80`'s four call/jump rows -> 100%. The same attribute on **data**
  can be worse: it lets clang fold a test into one `cmp qword ptr [rip+sym], 0` and lose an instruction, so keep
  plain `extern` declarations for data and use hidden on code.
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
- **Intra-unit calls**: a callee defined in the same source loses its relocation (`call 0x550` where the game has
  `call func_x`) unless it stays a separate, non-inlined symbol; `weak` + `noinline` on the definition keeps the
  `PC32` when `noinline` alone does not.

## Keep the build able to run

- **Scratch files go outside `src/`.** `build.ninja` lists every file under `src/` as an input of the rule that
  regenerates it, so a scratch `.c` that is created and then deleted leaves ninja unable to rebuild anything
  ("needed by 'build.ninja', missing and no known rule to make it") until `tools/configure.py` runs again.
- **A source that does not compile stops every unit.** One bad cast in one file makes ninja stop at that unit and the
  project measure as if the rest did not exist. Build the unit you changed (`ninja build/<version>/src/auto/<unit>.o`)
  before moving on.

## The three deltas left

These are the SDK's LLVM fork, not the flags; expect them on functions that hit them, and expect the permuter to close
the first:

1. **Order only.** The same instructions, scheduled differently: `movsxd` before `lea`, a `-1` hoisted after `mov
   rbx,rdi`, a float spill before the pointer add. The instruction multiset is identical, so the C is right and the
   compiler's scheduler is not — leave it and move on.
2. **Store merging.** clang 5.0.2 merges two adjacent 4-byte zero stores into one 8-byte store where the game keeps
   three separate 4-byte stores. Writing the fields through separate, non-adjacent statements does not always stop it.
3. **Branch idiom.** The game often keeps an early `mov al,1` and jumps to a shared `ret`, where clang 5.0.2 emits
   `setne al`. Same behaviour, different shape.

## What the code looks like

- Functions are `func_<address>` (`func_00000320`), data `D_<address>` (`D_9866D0`); write them with those names in C
  and the build gives them the original's symbols.
- The eboot is linked at 0 and loaded anywhere, so absolute addresses in the code are image offsets.
- A function's symbol size can include trailing `nop` padding; the delinker keeps the game's bytes, so a match is
  byte-for-byte against what the linker laid out.
- Imports are Sony's NIDs, resolved through a stub table; a call to one shows as a relocation to `func_<stub address>`
  until the NID is named.
