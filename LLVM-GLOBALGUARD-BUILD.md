# Building the global-guard clang 5.0.2

The PS4 projects here need a compiler that reproduces two things at once: the codegen of clang 5.0.2 (VEX.256 zero
idiom, no `vzeroupper`, frame-pointer shape) **and** the game's stack canary, which is the *global-guard* form —
the guard lives in `__stack_chk_guard` and is loaded through the GOT:

    mov r12, qword ptr [rip + disp]   ; &__stack_chk_guard, via the GOT
    mov rax, qword ptr [r12]          ; the guard value
    mov qword ptr [rbp - 0x30], rax   ; spill it
    ...                                ; at each exit:
    mov rax, qword ptr [r12] ; cmp rax, [rbp - 0x30] ; jne __stack_chk_fail

Stock clang 5.0.2 on a Linux triple emits the TLS form (`mov %fs:0x28,%rax`) instead, and `-mstack-protector-guard=`
does not exist before clang ~11. The guard model is chosen by the target triple in
`llvm/lib/Target/X86/X86ISelLowering.cpp`:

    static bool hasStackGuardSlotTLS(const Triple &TargetTriple) {
      return TargetTriple.isOSGlibc() || TargetTriple.isOSFuchsia() ||
             (TargetTriple.isAndroid() && !TargetTriple.isAndroidVersionLT(17));
    }

Returning false for Linux makes `X86TargetLowering::getIRStackGuard` fall through to `TargetLowering`'s global
`__stack_chk_guard`, which under `-fPIE` is GOT-loaded — exactly the shape above.

## The patch

```diff
--- a/lib/Target/X86/X86ISelLowering.cpp
+++ b/lib/Target/X86/X86ISelLowering.cpp
@@ -2000,6 +2000,11 @@
 static bool hasStackGuardSlotTLS(const Triple &TargetTriple) {
+  // LOCAL PATCH for a decompilation toolchain: force the classic global-guard
+  // model on Linux so -fPIE materializes the guard through the GOT instead of
+  // the %fs:0x28 TLS slot, matching PS4-era binaries built from these sources.
+  if (TargetTriple.isOSLinux())
+    return false;
   return TargetTriple.isOSGlibc() || TargetTriple.isOSFuchsia() ||
          (TargetTriple.isAndroid() && !TargetTriple.isAndroidVersionLT(17));
 }
```

One more hunk was needed only to build with GCC 11: `include/llvm/ExecutionEngine/Orc/OrcRemoteTargetClient.h:722`,
`readMem` returns `Expected<std::vector<unsigned char>>` and the caller wants `std::vector<char>`; changing the
return type to `Expected<std::vector<uint8_t>>` fixes it. `lli` only — no effect on codegen.

## Recipe (as built and verified here)

```sh
curl -sSLo llvm-5.0.2.src.tar.xz https://releases.llvm.org/5.0.2/llvm-5.0.2.src.tar.xz
curl -sSLo cfe-5.0.2.src.tar.xz  https://releases.llvm.org/5.0.2/cfe-5.0.2.src.tar.xz
tar xf llvm-5.0.2.src.tar.xz && tar xf cfe-5.0.2.src.tar.xz
mv cfe-5.0.2.src llvm-5.0.2.src/tools/clang          # LLVM_ENABLE_PROJECTS looks for a sibling
cmake -G Ninja -S llvm-5.0.2.src -B build \
  -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_PROJECTS=clang -DLLVM_TARGETS_TO_BUILD=X86 \
  -DLLVM_ENABLE_ASSERTIONS=OFF -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF -DCLANG_ENABLE_ARCMT=OFF -DCLANG_ENABLE_STATIC_ANALYZER=OFF \
  -DCMAKE_INSTALL_PREFIX=/opt/llvm-5.0.2-globalguard
ninja -C build -j8                                  # ~32 min on 12 cores, 8.8 GB of build tree
sudo mkdir -p /opt/llvm-5.0.2-globalguard && sudo chown "$USER" /opt/llvm-5.0.2-globalguard
cmake --build build --target install                # 709 MB
```

## Verification (what "correct" looks like)

1. `clang --version` is 5.0.2, target x86_64-unknown-linux-gnu.
2. A function with a stack buffer compiled with `-fstack-protector-strong` shows `mov r,[rip+…]` then `mov rax,[r]`
   then the frame store, `R_X86_64_GOTPCREL __stack_chk_guard` and `R_X86_64_PLT32 __stack_chk_fail` in the object,
   and **no** `%fs:` access.
3. The 21-byte zero-idiom function still compiles to
   `48 85 ff 74 0f c5 fc 57 c0 c5 fc 11 07 c7 47 10 7f 00 00 00 c3` — the patch must not disturb the rest of the
   codegen. Re-checked here: 14/25 of the hand-transcribed battery, the same misses as stock 5.0.2.

Linking a real PIE from these objects needs a definition of `__stack_chk_guard` (modern glibc exports only
`__stack_chk_fail`); that does not matter for decompilation, which compares objects.
