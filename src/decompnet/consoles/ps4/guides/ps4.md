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
