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
