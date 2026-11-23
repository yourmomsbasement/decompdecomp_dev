# Roadmap

What's planned for decompdecomp, roughly in order, and why. Every item keeps the rule that matters most: a match is
byte-identical after linking (ROM mode) or objdiff 100% (fork mode). Nothing here accepts code that only does the
same thing.

Every console gets every feature it can: one pipeline, ROM mode and project mode alike. A "—" in the table below is
a gap to close, each with its item here (CONTRIBUTING.md, "Every console gets every feature").

Want to work on one? Open an issue first, so two people don't build the same thing. CONTRIBUTING.md has the rest.

PS2 compiler packs now run on macOS: Sony EE-GCC 2.9/2.96/3.2 through an optional
local Linux runner, SN Systems EE-GCC and CodeWarrior through wibo. Physical Intel
Mac and retail-game verification remain planned; Apple Silicon checks use the
homemade PS2 demo. Arbitrary imported Make/Ninja builds in the VM remain planned.

## What works where

Each feature and the platforms it covers today. A — is a gap: pick one up (the notes under the table say where to
start). Keep this table true when a feature ships or reaches another platform. Each column's code is its console's
folder, `src/decompnet/consoles/<id>/`; a new console adds a column (CONTRIBUTING.md, "Consoles").

| Feature | N64 | PS1 | PS2 | GameCube · Wii | Windows PC | DS | Public decomps |
| :-- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Status | recommended | beta | beta | beta | beta | beta | the project's own |
| Start from your own copy | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | fork by address |
| Judge | link · compare | link · compare | link · compare | objdiff | objdiff | objdiff | objdiff |
| Machine drafts (m2c, Ghidra for x86) | ✓ | ✓ | ✓ | ✓ | ✓ Ghidra | — | by architecture |
| Autopilot: free matches from drafts | ✓ | ✓ | ✓ | ✓ | — | — | — |
| Drafts follow the header, made in the background; `redraft` | ✓ | ✓ | ✓ | — | — | — | — |
| C++ methods drafted as `Class::method` | ✓ | ✓ | ✓ | — | — | — | — |
| Twins: free matches from identical code | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Permuter on near misses | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Hand-written assembly set aside | ✓ | ✓ | ✓ | ✓ | — | BIOS call stubs, pieces of functions | MIPS, PowerPC, ARM's BIOS stubs and pieces of functions |
| Pieces of functions joined to their function | — | — | — | — | — | ✓ (`recover.merge_pieces`) | — |
| Agents' verdict "no C makes this" (3 agents of 2 models: set aside, red in the map) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Check again (and before helpers start, once after each update) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Kernel stubs set aside and named | — | set aside (BIOS calls) | ✓ | — | — | — | — |
| Function-end reports (`report_boundary`) | ✓ | ✓ | ✓ | — | — | — | — |
| Big-function briefs (repeated calls, macros, long look-alikes) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Big functions in parts (`compile_part`, several agents with `claim_part`) | — | — | — | ✓ | ✓ | ✓ | ✓ |
| Compiler ranges and suggestions | ✓ | ✓ | ✓ | the project's build | the project's build | per source file (`try_compiler`, Settings) | the project's build |
| Flag search (`search_flags`) | ✓ | ✓ | ✓ | the project's build | the project's build | the project's build | the project's build |
| Compiler map and the judge's survey | ✓ | ✓ | ✓ | — | — | — | — |
| Range checks: a range applies after its neighbours confirm it | ✓ | ✓ | ✓ | — | — | — | — |
| C++ classes from the code (`get_class`) | ✓ | ✓ | ✓ | — | — | — | — |
| Struct layouts from loads and stores (`guess_struct`) | ✓ | ✓ | ✓ | — | — | — | — |
| The game's own debug symbols: names, types, prototypes | — | — | ✓ symbol table, STABS | — | — | — | — |
| Twins across your games | ✓ | ✓ | ✓ | — | — | — | — |
| Shared knowledge (two models agree) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Polish matched code (still matching) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Real names for the game's symbols (`rename_symbol`) | ✓ | ✓ | ✓ | ✓ | — | ✓ | dtk and ds-decomp projects |
| Save and restore (`progress/`, Open a saved project) | ✓ | ✓ | ✓ | the project's git; best attempts saved | the project's git; best attempts saved | the project's git; best attempts saved | the project's git; best attempts saved |
| Reimport: a new split, matched work kept (Settings) | ✓ | ✓ | ✓ | — | — | — | — |
| Rebuildable export | ✓ | ✓ | ✓ | the project | — | the project | the project |
| Rooms: decompile together | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Where to start on a gap:
- **PowerPC (GameCube, Wii) and x86 (PC)**: class recovery and the compiler map work on a project's objects in fork
  mode, not on a ROM; the readers are the part to write (CodeWarrior's vtable pointer is often not at offset 0).
- **Windows PC**: autopilot needs drafts that compile (Ghidra's output doesn't, as it is); the export needs a linker
  story (the project is delinked COFF objects).
- **Function-end reports** cut MIPS functions (`engine._cut`).
- **Big functions in parts** replace one part's C in the function's source file; a ROM project's candidate lives in
  no file. It needs the working copy `compile_patch` waits for there (the agent's latest source of the function,
  judged by linking), and ROM diffs' rows mapped to the target's instructions (`function_parts.rows_by_part`).
- **Struct layouts** read MIPS loads and stores (`arch/mips_fields.py`); another processor adds its reader as its
  `Arch.field_accesses`, and structs.py does the rest. Still open on MIPS: the PS2's fifth to eighth arguments
  (t0-t3), and a pointer into the middle of a struct handed to a callee (`&this->pos`).
- **Twins across games** and **restoring a save** work on ROM projects (`app/twins.py`, `app/saves.py`). In project
  mode a project's matched C is its git repository; twins across games pair two projects' target objects
  (`Twins._across_projects`), the same path for every project-mode console (PC proved it; GameCube, Wii and DS use
  that code, but weren't run on two of their games); what's missing is opening a save's best attempts in a new copy
  of the project.

## Recently done

- **A share of helpers on mid-size functions.** Small functions went first for every helper: on a PS4 game 1 of 223
  claims in 1.6 hours was of 60 instructions or more, while 6,210 functions of 40-100 had never been tried. Every
  third helper active in a project (by name, so the share holds as helpers come and go) now takes a function of
  40-100 instructions first, with the default strategies; release cooldowns, waits and model routing still come
  before it, and a strategy asked for by name keeps its order (`Network._mid_helper`, `MID_BAND`, `MID_SHARE`).
  And AgentRouter's "user quota is not enough" is known as out of credit: its helpers pause, saying to top it up,
  where they restarted 28 times in 1.6 hours.

- **The permuter judges variants outside the project's tree.** Each variant was judged like an agent's compile, in
  the tree: the unit's baseline built, the variant built, the file put back and built again, beside the helpers'
  builds (on a PS4 game, about 5 variants a 60-second turn). Now each is built from a scratch copy of its unit
  (`Console.build_unit_with`, nothing in the tree written) and diffed; only one that beats the function's best goes
  through the project's judge, which still protects every match and records it. Measured on a copy of that game:
  88 variants a turn against 17. Consoles that can't build outside their tree (everything but PS4 so far) judge in
  it as before. decomp-permuter also reads files whose structs carry attributes again (`_without_bodies`).

- **Switches can match on PS4.** clang puts a switch's jump table in the object's own read-only data; the game's
  linker put it among the game's, so the delinked target loads it from a game symbol (`D_009889E8`) and the two loads'
  relocations never compared equal: 391 open functions dispatched through such a table, and none had matched. The
  template's `tools/jump_tables.py` (run by `compile.py` after `bind.py`) names our load after the game's table only
  where both tables send every case to the same offset of the function, read from the game's image; a table with any
  case elsewhere is left alone. On the dev game, 8 functions matched at once (every match kept); the rest now can.
  Projects get it with their build tools' update at Check again (`compile.py` carries a `TOOL_VERSION`).

- **A compiler per PS4 unit, on evidence, documented.** A PS4 game's units aren't all built alike: 820 match only with
  `-march=btver2`, and some only with an older upstream clang. Clang 3.8.1 and 3.9.1 are pinned (`toolchains.toml`); a
  unit's entry in `tools/compiler_units.json` can name its compiler, with why and its evidence, and the template's
  `compile.py` builds it so (an older clang on the PS4 target, which reads the stack guard from memory as the game
  does). A unit switches only when two of its functions match only with the other compiler and none of its matches is
  lost (`Console.compiler_evidence`; DS keeps one): `try_compiler` keeps a single match as evidence until a second
  completes it, and `compiler_survey` builds every unit with every compiler outside the project's build, switching
  only those that pass, rebuilt and checked again in the project. Every unit with settings of its own is listed in
  the project's `docs/build-settings.md`, with why, the functions that show it and the commit that set it, written
  again and committed with every change; each function's brief says its file's settings and why. Older PS4 projects
  get the new `compile.py` at start, kept only if every match still holds.

- **The PLT is set aside, and repeats survive other files' edits.** An x86-64 ELF program's PLT header and its
  imports' lazy-binding steps (`push <index>; jmp <header>`) are the linker's: on a PS4 game 425 of its open
  functions, now out of agents' way (`x86.asm_reason`). The entries' `jmp [rip+GOT slot]` stay open: C makes them
  (`D_00D9E160[0]();` matched four). And an edit of a unit's own source no longer clears every agent's repeat check:
  a fork compile's key holds its unit's whole text. On that game an edit came every 39 s, and 4,657 compiles (12%)
  were an agent's own repeats built again. The open jumps get a tiny-function template, a tail call through the
  pointer (`void f(void) { slot[0](); }`), judged as every template is; direct tail jumps read objdiff's addend, which
  it writes as text, and so were never recognized before. Autopilot's tiny-function stage runs passes in a row while
  they still match (one pass of 100 a run before), and the project page shows the helpers holding parts of big
  functions and how many big functions are open and tried. Where m2c can't read the code, autopilot tries the
  project's cached Ghidra drafts, in the console's type names, for functions under 10 instructions: measured on a PS4
  game, 3 of 400 such drafts matched and none of 240 larger ones (most don't build: the globals they use aren't
  declared in the file); the easy ones its own tools/drafts.py had already taken (249).

- **Big functions for every model** (source-tree projects; briefs everywhere). A function of 400 instructions or
  more gets a brief that says how big ones matched (one-letter macros for the runs of the same call), names the
  calls that repeat in its code and the macros its file already has, and shows a matched look-alike's or twin's C
  however long (up to 32 KB; it was cut at 3,000 characters). Its target is cut into parts of 60-240 instructions at
  its blocks (`function_parts.carve`: a case after the previous case's jump, a block after a return); the C marks
  each with comments (`/* part N */ ... /* end part N */`), and `compile_part(function, part, code)` replaces one
  part's C and judges the whole function as always, answering with that part's diff rows and every part's count of
  differing rows: 100-200 instructions' worth per call, which any model can send. Several agents work on one
  function at once: `claim_part` takes a marked part beside the function's owner (a claim of a held big function
  says which parts are free, and so does a claim that finds nothing); while others work on it, a part's change is
  kept only when the score gets no worse and no other part's rows grow (a preview first), and a whole-definition
  compile may not change a part someone holds. `big_function_report` measures it: big functions tried, compiled
  and how far, and their compiles this week (with compile_part) against the week before.

- PS4: recognize RIP-relative references into memory-only BSS without treating BSS as readable file data; replace obsolete compiler-blocker guidance with verified local techniques.

- PS4 target integrity: correct ELF symbol indices, one-byte branch relocations, explicit function extents and shared-tail ownership; require relocation identities when judging matches. Legacy count-only maps fall back to Ghidra.

- **Names in project mode.** rename_symbol renames a function or a global in a project too: in the game's symbols
  (DS: ds-decomp's symbols.txt; GameCube, Wii: dtk's) and in every source and header file, kept only when the game
  still rebuilds byte for byte and every match holds. Polishing's names were local only before (func_0205d0f4,
  data_0222f194 stayed). Windows PC's names come from addresses (`names.py`): it says it can't rename yet.
- **Polishing on every console.** Helpers that make matched code readable (real names, types, a comment) worked on
  ROM projects only: a DS game at 57% had nothing to choose. In project mode a rewrite goes through the project's
  own file and is judged like any compile of a matched function: committed when it and the file's other matches
  still match, else the file is put back. Helpers there keep the function's name and describe types in the project's
  headers (edit_file).
- **Agents join what the rules miss** (project mode). `join_functions(function, with_function, reason)`: once 3
  agents of 2 models say two functions are one the analysis cut, the console joins them (DS: `recover.join_functions`,
  across two source files too), checked as an analysis is (the game rebuilt byte for byte, else taken back), never a
  matched function; a refused join is remembered. Check again only ever joins, so what agents joined stays. A
  console that can't join yet (GameCube/Wii) isn't offered `join_functions`, and a join is refused before any votes
  are recorded, so nothing goes into `joins_refused`.
- **What no C makes is out of agents' way, and progress counts the rest.** On a DS game 159 of 557 open functions
  were pieces of others (ds-decomp's symbols in the middle of functions: a lone epilogue, a register save running on
  into the next symbol, a lone `bx ip`), each given up on by five to seven agents in turn. ARM code that pops pc
  without having pushed, jumps through a register its caller set, uses r4-r11 before saving them or never returns
  is set aside (`arm.piece`), and a
  DS project joins such pieces to the function they belong to (`recover.merge_pieces`, checked by dsd): on that game
  119 joined, 4 functions matched at once with the C agents had written, no match lost, the ROM still identical.
  What shapes can't tell, agents can: `release_function(..., not_c=true)` with the reason, and once 3 agents of 2
  models agree the function is set aside, red in the map, until the user gives it back. Percentages and counts leave
  set-aside code out. **Check again** (About this project) looks at a project with what the running version knows;
  it also runs before helpers start, once after each update.
- **PlayStation (PS1) games from your own copy** (beta): a disc image (BIN, ISO or CHD; SYSTEM.CNF says which
  program it boots) or its PS-X EXE, with the disc's overlays. Functions are built as PSY-Q games were: GCC 2.5.7,
  2.6.0, 2.6.3, 2.7.2 (or its CDK build), 2.8.0, 2.8.1, EGCS 2.91.66 or GCC 2.95.2, then maspsx, which does what
  Sony's assembler ASPSX did, then GNU as; the import tries them on the game’s code. BIOS call stubs and GTE
  code are set aside as assembly. The demo game matches function by function and rebuilds byte for byte; no real
  game is tested from start to end yet. A disc whose boot program is a loader gives the program it loads
  (`exe.loaded_program`), and where splat's guess at the code misses the entry point (a loader that is mostly a
  picture: 1 function found), .text is where the code's returns and frames are (137). Overlays inside an archive file (a
  game's areas, each with its code, one after another) are found too (`overlays.embedded`): on one PS1 game, 77
  overlays in a 68 MB file, 26,483 functions besides the program's 2,398.
- **Nintendo DS games from your own copy again** (beta, #23): a ds-decomp project for the ROM, as in 1.0.2, now with
  all 24 CodeWarrior for DS versions and a version per source file (`config/<version>/arm9/file_compilers.json`, as
  DS decomps keep it). `try_compiler` switches a function's file to another version, or tries them `"all"`, and
  keeps the one it matches with when the file's other matches still hold: a game whose code mixes 1.2 and 2.0
  (1.2's `ldmia {..., lr}; bx lr` epilogue) no longer sticks at 80-95%. Machine drafts are the gap: a ds-decomp
  project keeps no assembly for m2c to read.
- **DS functions only pointers reach.** dsd's analysis follows calls, so callbacks, state tables and everything they
  call stayed outside any function (77% of one game's main program), and functions calling across those gaps never
  reached 100%. `consoles/nds/recover.py` finds them by their starts, adds their calls and pointers, and drops what
  dsd refuses to delink: 1,784 more functions in that game, every match kept. Projects made before it get them once
  when the app starts (`ForkEngine.reanalyse`): kept only when the rebuilt ROM is unchanged, and the project's
  activity says so. dsd also ended `.text` at the last function it found, and the 45 KB of code after it was
  `.rodata` to it, its 19 calls from `.text` unrelocated: `.text` now ends where that code does
  (`recover.extend_text`), with 207 more functions and 13 source files of their own. 2,987 functions in all.
- **A DS game rebuilt byte for byte.** The link stripped what nothing points at, and nothing points at an overlay
  holding only data (the game loads it by number): those came out empty, every overlay after them moved, and the
  rebuild differed in 209 overlays and every pointer to them. It links everything now (`-nodead`), and the header
  gets the copy's secure area checksum (of the area encrypted, which a dump no longer is; dsd left it 0). The game
  that differed rebuilds identical, its 1,784 recovered functions included. Projects made before get their build
  fixed at start (`scratch.write_tools`, kept unless an identical rebuild stops being one).
- **The permuter's real rewrites in forks.** decomp-permuter got no header in a fork, so it refused every
  function ("Syntax error in base.c": `u16` was no type it knew) and only the six simple rewrites ran, in every
  fork and on DS. It now reads what the function's file includes and declares.
- **Model routing.** Each model's share of its claims that matched, in the project, tells the models at work
  apart (on an N64 game free models matched 14-25% of what they claimed, stronger ones 42-77%). A model well behind
  the strongest one at work leaves functions given up on three times to it, and the strongest takes those first;
  near misses already go to the permuter. Nothing changes until two models have 20 claims each.
- **Helpers' effort.** The launcher's Effort, opencode's until now, is every helper's: Claude Code's (`--effort`,
  Low to Max with Extra high), Codex's (`model_reasoning_effort`, Minimal to Extra high) and the API helper's
  (OpenAI's `reasoning_effort`, Minimal to High, for servers whose models reason). A model at another effort is a
  helper of its own (`claude-opus-xhigh-1`), so its matches and spend are counted apart.

- **Agents try before they release, and say what a function waits for.** On a large x86 game agents released
  functions seconds after claiming them, untried (12,565 releases, median 5 s). release_function now answers a
  release before compile_and_diff's move-on rule once with what to do; a release without a compile doesn't count
  against the session; `needs=<function>` records a prerequisite: the function waits (claimed last), the one it
  needs goes first and is handed over, and the match brings the waiting one back first. Notes saying "already
  matched" on a function that doesn't match are marked out of date.
- **Rooms that say what's happening** (1.0.4-alpha). A guest waiting for the host sees "Waiting for the host to let
  you in" over the project, not an empty one and a red "Room offline"; then "Getting the host's work", counting down.
  A new guest starts at the room's newest event instead of replaying its history into the feed and notifications,
  and one that was away catches up a page at a time (the sync started from nothing at every start). Each computer
  has a colour on its agents' names, the same on every screen, and the Agents tab lists what the room's other
  computers' agents are on. A git project's join waits to be let in (with approval on, it failed), and a declined
  or removed guest is told so.
- **Room presence during slow syncs.** A separate heartbeat keeps members online while importing the host's work
  or rebuilding a source project. Sync failures and recovery are logged, and syncing continues automatically
  after a temporary connection failure. Applies to ROM and project rooms on every console.
- **Bring saved ROM progress into a room.** When joining with a game already imported, saved matches the host has
  not matched yet are submitted for the host to verify. Better unfinished attempts are shared when both projects
  use the same compiler settings; functions whose layout or name differs are skipped.
- **A standalone app.** Its own window (GTK and WebKitGTK) and a tray icon, so closing the window doesn't stop
  helper agents; **Quit** ends everything the app started. Only helpers the app starts work on projects: the old
  `decomp mcp` bridge is gone, and the first start removes its registrations from your agents. A development copy is
  a separate app (`~/Decomp-dev`) that never touches the installed one.
- **Helpers that restart by themselves**, with backoff; on a usage limit they wait until it resets. The API helper
  (any model with an API key) is the main helper: the app runs its whole loop.
- **One notification centre** (the bell) for every project, and **Report a problem** on every page.
- **Save and restore** (ROM projects): the work as plain files in `progress/`, every few minutes; **Open a saved
  project** rebuilds it on another computer from your own copy, and judges every function again.
- **Reimport a ROM project** (Settings): replace the split from a newly selected copy, keeping matched C only where
  its target is unchanged, plus notes, settings, saves and the names agents gave (functions are paired by address).
  A matched function the new split doesn't have keeps its C in a note. PC projects reimport too (Ghidra again);
  other project-mode consoles can't yet: their project's own analysis makes the functions (DS's Check again redoes
  what decompdecomp adds to it; GameCube/Wii's doesn't yet: Next).
- **Polish matched code** (ROM projects): helpers give matched functions real names, types and a comment; a change
  counts only when the function still matches. The project shows matched and readable.
- **Shared knowledge**: what agents learn about a game reaches every agent once helpers of two different models
  agree on it.
- **Twins across your games**: code matched in one of your games is tried on the identical functions of the others
  (libraries, the compiler's runtime). No agent time; nothing leaves your computer.
- **A real permuter**: decomp-permuter rewrites near misses on the parse tree, weighted per compiler, on spare
  cores at a low priority. On nine near misses made from the demo ROM's functions it matched all nine, the old
  regex rewrites five. They stay as the fallback, for C decomp-permuter can't parse.
  A user can also start a bounded deep search for one unmatched function. It shares the background worker pool,
  honours the time and variant limits, and records only improvements that pass the normal judge.
- **Better drafts**: m2c drafts every function in the background, from the header declarations its code uses, and
  again when they change; `redraft` runs m2c with an agent's own declarations; methods of known C++ classes are
  drafted as `Class::method`. On GTA San Andreas, 37 of 40 sampled drafts read the header (none did before).
- **Flag search** (`search_flags`, MIPS): each flag of the compiler changed once, up to 12 builds.
- **PS2 debug symbols** (#3): names and sizes from the symbol table; from STABS the source files as units,
  prototypes, parameters and locals in the brief, every type and global in the header; the same from the DWARF 1
  CodeWarrior writes (.debug, .line), with the line each function starts on. Big headers: the brief
  carries only the declarations the function uses.
- **PS2 kernel calls named**: the stubs (`addiu v1, N; syscall; jr ra`) get the kernel call's name: 154 in GTA San
  Andreas, 129 in Ridge Racer V.
- **Consoles as packages**: one folder per console (`consoles/<id>/`), a template and a conformance test. PS1 and
  GBA were public decomps only after the rewrite; PS1 has its package, and GBA's is back with its header check,
  its demo, its splitter, its judge and its export.
- **macOS app**: a native Cocoa/WKWebView window with the private window user agent,
  native open/save panels, downloads, Edit/Reload/Quit menus and the page's close confirmation. A uv source
  installer creates a Finder app in `~/Applications`. Intel and Apple Silicon CI exercise portable regressions
  and the hidden native window. KMC GCC/binutils require Rosetta on Apple Silicon; unavailable Linux-only
  compilers remain unavailable. PS1 GCC 2.6.3 builds natively from pinned source on Intel and Apple Silicon Macs
  with Xcode Command Line Tools; GCC 2.7.2, 2.7.2 CDK and 2.8.1 use pinned native Apple Silicon builds with maspsx;
  the other PS1 GCCs and EGCS 2.91.66 need Linux, or Windows through WSL 2. The assembler helper uses the app's
  Python rather than Apple's older system Python. Mac workers use per-thread QoS so they don't lower the whole app's
  priority.
- **Linux packages**: a `.deb`, an `.rpm` and an AppImage with their own Python, built for each release.
- **Windows native app**: a WebView2 window, Start menu and desktop shortcuts, and a native tray icon.
  A custom native caption follows the app's theme, with Windows resizing, snapping and drag-to-restore. Scrollbars
  follow the page's theme too. Closing asks whether to keep helpers working. Kernel jobs end helper children after
  a session or an app crash. Tools have pinned Windows builds; Linux-only compilers use WSL 2 with Linux-side tool
  and build caches. Windows CI checks the native and portable code beside the existing Linux checks.
- **Struct layouts from loads and stores** (MIPS: PlayStation 2, N64; x86: Windows PC, Xbox). Every load and store
  through a function's pointer arguments, the pointers loaded from them and globals, made into types along the call
  graph: the brief names each pointer argument's likely fields (offset, width, how many functions agree),
  `guess_struct` writes the struct as C, says where else the type flows and which name matched code already gives it.
  Checked against the structs agents gave matched functions on GTA San Andreas: 95% of the offsets it proposes from
  other functions are right. x86's reader is `arch/x86_fields.py`; PowerPC and ARM need theirs (`Arch.field_accesses`).
- **Range checks** (MIPS ROM projects). An agent's "this range was built by another compiler" is a hypothesis: the
  judge tests the functions next to it (their matches, agents' attempts, the survey, habits), and where nothing
  settles it the nearest neighbour goes to an agent as a task. A range applies once a second function matches only
  with its compiler; refuted neighbours on both sides make it a quirk (dismissed, with a note on the function). The
  applied ranges are checked again the same way, by themselves. On GTA San Andreas's 72 ranges: 12 confirmed, about
  25 resting on one function, about 36 with no function whose C needs their compiler.

- **Compiler map** (MIPS: PlayStation 2, N64): which compiler built each part of the game. Measured on
  GTA San Andreas, compilers' habits only rule compilers out (-G0 code never reads through `$gp`; its CodeWarrior
  code never uses branch-likely), so what rules a compiler in is the judge: the survey builds agents' C for unmatched
  functions with each compiler the project uses, and C that matches with another compiler (and not today's)
  confirms it. Confirmations spread over their neighbours (one object file is one compiler) up to the furthest
  evidence; a run big enough becomes a suggestion, checked against every matched function. The ROM map shows it
  (Compiler view); agents read it with `compiler_map` and in `get_function`.
- **C++ classes from the game's code** (MIPS: PlayStation 2, N64). Constructors store their class's
  vtable into the object; from those stores decompdecomp finds each class, its virtual functions, its base class and
  every field its functions touch (offset, type). Agents read them with `get_class`; `get_function` names the fields
  of `this` a function uses. On GTA San Andreas: 507 classes, and the recovered entity layout (position at +4, matrix
  pointer at +0x14, model index at +0x22) is the one found by hand. Why: functions with virtual calls matched 7% of
  the time against 62% for the rest, because agents guessed the layouts.

## Next

### Your own compiler for GameCube, Wii and DS

Their projects (dtk-template, ds-decomp) take CodeWarrior by version from the community's compilers archive, which
holds every build known, so no game has needed one of its own yet. When one does: the project's build gets a
compilers folder of the user's beside the archive's (dtk-template's configure.py reads one), and the console lists
its versions (`Console.kit_compilers`), as Windows PC does for Visual C++ through `--own`.

### GameCube, Wii and DS: the rest of the table (#232)

PC got these first; these are the columns still short. In progress this round: a DS autopilot that says why it has
no drafts, and the DS latest/best attempt switch (PR #254). The rest:

- **Drafts in project mode** (`app/drafts.py`, `app/forkengine.py`). The background drafter starts only for a ROM
  project or a console whose `own_drafts` is set (PC), and `redraft` refuses a project-mode console that isn't PC: a
  GameCube or Wii m2c draft is made once and never follows the headers, and DS has no draft at all, because
  `forks/drafts.py` looks for assembly in the project tree and a ds-decomp project keeps only delinked objects. Make
  the drafter cache and remake m2c drafts when a header changes, let `redraft` pass an agent's declarations to m2c
  (`seeds.generate_from_asm`), and for DS read the unit's target object (`ForkProject.unit(...).target`) or write a
  temporary `.s` for m2c. The DS autopilot's "no drafts" guard is in progress this round.

- **C++ methods as `Class::method` on GameCube, Wii and DS.** The rewrite is `Arch.method_draft`, which only x86 has
  (`arch/x86.py`), and the class reader is MIPS and x86 (`classes.py`); both need the PowerPC/ARM readers in "C++
  classes and struct layouts for PowerPC and ARM" below. Then `_method_draft` (`app/forkengine.py`) and the brief's
  class lines work without a console-specific branch.

- **Check again and Reimport on GameCube/Wii; Reimport on DS** (`Core.recheck`, `Console.on_start`,
  `Console.reimport`). Check again calls the console's `on_start`; GameCube/Wii keep the default no-op, so they only
  set aside code no C makes, and `reimports` is PC's alone. Give `consoles/gc_wii/scratch.py` an analysis step like
  DS's `consoles/nds/recover.py` (dtk's splits and assembly, through `ForkEngine.reanalyse`), then a reimport that
  runs dtk again and pairs functions by address; DS's reimport runs `dsd` and `recover.py` again.

- **Compiler map, survey, range checks and flag search on GameCube, Wii and DS** (`app/filecompilers.py`,
  `app/rangecheck.py`, `ForkEngine.search_flags`). The map and the judge's survey work over source files where
  `Console.file_compiler` is set: a GameCube, Wii or DS program names no compilers, so the survey tries up to six of
  the project's own versions, nearest its own first, and range checks use the same candidates; a public fork's build
  decides alone. Their agents are offered `compiler_map`. `search_flags` is not offered, because `Console.file_flags`
  is `None`: start with per-file flags in dtk's `configure.py` and DS's build config, then `file_flags`/`flag_variants`
  as PC's.

- **Pieces of functions on ROM projects and GameCube/Wii** (`Console.join_functions`, `rom/split.py`,
  `arch/mips.py`, `arch/ppc.py`). DS (`recover.merge_pieces`) and PC (`scratch.merge_pieces`) join what their
  analyzers cut, and their agents are offered `join_functions`; N64, PS1, PS2 and GameCube/Wii have neither a piece
  rule nor a join (GameCube/Wii refuse one before any votes and don't offer the tool), though a saved layout can be
  joined (`rom/split.join_saved_functions`). Add `Arch.piece` for MIPS and PowerPC, then a ROM project join that
  keeps the split's layout and rebuilds; GameCube/Wii join in `consoles/gc_wii/scratch.py` as DS's does.

- **Kernel and SDK library code set aside and named (N64, GameCube, Wii, DS)** (`Console.sets_aside`,
  `consoles/*/scratch.py`, `arch/arm.py`). PC and Xbox set aside the code their compiler and libraries generate; these
  consoles count their SDK's linked-in library code as the game's, and DS's BIOS calls are set aside as assembly
  without their names. Start with known-library signatures or a hash list (`Console.library_candidate`, `kits/` for
  SDKs) and name the DS `svc` numbers as PS2's syscalls are (`consoles/ps2/syscalls.py`).

- **The game's own debug symbols on N64, PS1, GameCube, Wii and DS** (`Console.debug_info`/`debug_of`,
  `consoles/ps2/debuginfo.py`, `consoles/pc/debuginfo.py`). PS2 reads STABS/DWARF 1 and PC its PDB, map and COFF
  symbols; these consoles return nothing. Start with a map or SYM file the user's copy has (the common retail
  formats carry none), then the DWARF a full ELF or development build keeps, and give the function's brief its
  prototype and types through `debug_brief`.

- **Saved best attempts in a new copy (project-mode games)** (`app/saves.py`, `_save_best` and `_judge_saved`). A
  project's save writes each open function's closest attempt, but restoring a save judges the matches and leaves
  `best/` behind. Open those attempts in the new copy too, by function name, and keep them as the functions' best.

### PlayStation 3 from your own copy

ROM mode, as the PS2 and PSP: you give decompdecomp the game's program decrypted (EBOOT.ELF: a SELF is refused, it
never decrypts anything), it splits it (the module descriptor's functions first, then calls and prologues), and the
judge links each candidate at its address and compares what each instruction refers to (`arch/ppc64.py`: TOC slots,
function descriptors, the call-site TOC restore). Its imports are named from their NIDs and its kernel calls from
their numbers (PSL1GHT's lists, MIT), and their stubs set aside. It builds with the free ps3dev GCC (the demo) or your
own SDK's compiler (the `ps3sdk` kit: the game's `.comment` stamp picks the copy). Every feature of the table ran on
its demo (`projects/demo-ps3`), and a decrypted retail game (22,482 functions, SDK 2.5.0) imported whole: its module
descriptor (Sony's 8-byte pairs), its import stubs (all 194 named), its nameless sections and its SDK read as they
are, and its functions matched through the judge with Sony's GCC rebuilt from its GPL source (SDK 2.5.0, the
game's own), a getter of a global among them: that game reached its globals through GCC's minimal TOC (r30), which
the import reads off the code and builds with (`-mminimal-toc`), and its linker didn't narrow any TOC load. What's left:

- **The period compiler without an SDK.** `packaging/ps3-gcc/` rebuilds Sony's `ppu-lv2-gcc` (SDK 1.9.0, 2.5.0, 2.7.0) from
  the GPL parts of its published source; its `toolchains.toml` entries wait for it to be published.
- **Drafts.** m2c has no PPC64 target: a ROM-mode Ghidra draft (as PC's) would give PS3 drafts, autopilot's draft
  phase and `redraft`.
- **The processor's readers.** `arch/ppc64.py` has no field accesses (`guess_struct`), class reader (`get_class`),
  piece rule or compiler map yet; the unstripped program's DWARF types and prototypes aren't read either.
- **Later:** the rebuildable export, PRX modules, SPU programs.

### Xbox: what's next

The original Xbox is a beta PROJECT-mode console: decompdecomp reads your default.xbe (or an .xiso / Redump .iso),
makes it a Windows program at the same addresses, and from there it is the PC path (Ghidra, COFF objects, objdiff,
`ninja rebuild`, which writes your default.xbe back byte for byte). With your own XDK set up in Setup (a kit:
`kits/`), its library code in .text is set aside and a game of an early XDK builds with that XDK's own Visual C++.
On Windows a game of Visual C++ 7.1 needs your XDK: the msvc pack's 7.1 doesn't start there, the XDK's copy does.
Three retail games imported and rebuilt byte for byte (thousands of functions each). All three were built with
link-time code generation (/GL, /LTCG), as most Xbox games were: the import tells it from the program (or the import
dialog says), units build in harness mode (`tools/ltcg.py`: the unit, a driver calling its functions as the game's
call sites do, and stand-ins, linked /LTCG by the compiler's own linker through wine), briefs show each function's
convention, `standins_for` writes a starting harness, and a register-convention function of a retail game matched
through its driver. Briefs also say when a function is a member function (its `this` in any register) and of which struct,
and an attempt whose function got another convention than the original's says which registers differ. What's next is
real games: a whole one rebuilt from C, then the console's status can say recommended.

### Game Boy Advance from your own copy

ROM mode, as 1.0.2 had it: luvdis (MIT) finds the functions, agbcc (pret's GCC 2.95 rebuild) compiles the C, the
ARM/Thumb judge links each candidate at its address and compares the bytes, and the export rebuilds the cartridge.
The console, its header check, its demo, the luvdis splitter, the ARM/Thumb judge, editing and the export are back
(`consoles/gba/`, `projects/demo-gba`): the demo imports, all 123 functions are found after splitting/adding the
merged ones, 107 of the 114 with C match through the engine, and the export rebuilds it byte for byte with 71
functions as C. What's left:

- **Compilers and ranges.** agbcc's release also has `old_agbcc` and `agbcc_arm`, but the console lists only `agbcc`:
  selecting them per file and the compiler map/range checks/suggestions (`try_compiler`, `search_flags`) are MIPS
  only. GBA games built with another of the three need them.
- **Free matches.** ARM/Thumb look-alikes and twins, and the permuter's GBA weights (`agbcc` now maps to GCC's
  weights, but it wasn't run here); twins across games need the per-function Thumb mode too.
- **Code readers.** C++ classes (`get_class`), struct layouts (`guess_struct`, no `Arch.field_accesses` for ARM) and
  the game's own debug symbols aren't GBA readers yet.
- **Pieces.** The analyzer finds pieces (`arch.arm.piece`), but ROM projects have no join tool; `report_boundary` and
  saved-layout joining work at 2-byte boundaries.
- **Tidy** a source file is project mode only; a GBA ROM project's C is the database's.

Only agbcc's free tools are needed; the BIOS, Nintendo's SDK and the cartridge logo never are (the demo's logo is
blank).

### Newer Windows games (Visual C++ 2012, 2013, 2015 and 2019+; 64-bit)

PC projects build with Visual C++ 6.0 to 2010 and 2017 (phases B and C are done, and 2017's games: Recently done). The
import already tells a newer game apart (its build, /GL, 64-bit, DRM); what's left, one phase at a time. Visual C++ 2017's
compilers are installed (`msvc2015`, Recently done), build 2015's and newer's games (the nearest the packs have), and can
be chosen per file. A game built with a Visual C++ no pack has (2012, 2013, 2019, 2022, or another build of a packed
version) builds with your own copy of it when you have one: the `visualcpp` kit (`kits/`), set up in Setup from your Visual
Studio's VC folder, matched to the game's Rich header by build (2015 and newer through wine); without one it builds
with the nearest pack's compiler, which little of matches until decompdecomp has that version. A new version also joins
a pack (2015's Update 3 is to join `msvc2015`, beside 2017) or comes in one of its own (`msvc2019`...: a `[msvcYYYY]` entry
naming its SDK in `headers`), and goes into `RICH_BUILDS`, so its games build with their own compiler instead of the nearest.

- **What 2005-2010 still lacks.** The newer exception-handling tables (`__CxxFrameHandler3`) need the stub rules
  tested again (a game rebuilt with 2010 tools: 6,583 stubs found; a 2005 game built without C++ exceptions: none).
  2005's RTM isn't in the pack: its Rich header build is SP1's (50727), so SP1 builds both, and a game of 2005-2006
  may need RTM (decomp.me's `msvc8.0`). wibo 1.2.0 doesn't run these compilers' linkers (`lstrcmpiW`, added to wibo
  after it), which only the demo's build needs. On a Windows computer the pack is untested: there the compilers need
  their C runtime (msvcr80, msvcr90 through side-by-side manifests, msvcr100), which wibo has built in.
- **D, 2012-2013, then 2015, then 2019 and newer.** The same, per generation: SSE2 float code by default from 2012,
  and one toolset for 2015's updates (Update 3 brought the new SSA optimizer). Until then 2015's games build with
  2017's 14.16, 2019 and newer's too, 2012-2013's with 2010 SP1. A game linking the C runtime from DLLs builds with
  /MD from 2015 on; older games doing so (msvcrt.dll, msvcr71...) don't yet. /MD is the whole project's, from the
  program: a DLL of the game's built the other way (/MT where the program has /MD, or the reverse) builds as the
  program does, and its calls into the C runtime may not match; the import says which. Giving each program its
  own needs per-file flags that a file keeps through split_file (it keeps its compiler, but not its flags: /Oy- is
  lost the same way).
- **E, whole-program optimisation.** With /GL the linker generates the code for the whole program, so one file's
  object says nothing about the program's bytes. Find out whether some functions can be judged by compiling their
  file with /GL and linking it with /LTCG against stubs; until then a mostly-/GL game isn't started.
- **Visual C++ 2017 on Windows, and /Zi under wine.** On a Windows computer the 2017 pack is untested. Under wine,
  /Zi with /FS fails (C1902: no mspdbsrv), which no project's build uses.
- **F, 64-bit games, what's left** (64-bit games start: Recently done). Each Visual Studio version's own 64-bit
  compiler, with its pack (2017's builds every 64-bit game until then); your own Visual C++ copies (the `visualcpp` kit)
  are read for their 32-bit compiler only, so they don't build 64-bit games yet. Visual C++ 2019 and newer write a
  64-bit C++ function's exception data in another form (`__CxxFrameHandler4`'s, their default), which `funclets_x64`
  doesn't read yet: such a game's funclets stay functions of their own, which no C makes. Autopilot's exact drafts of
  tiny shapes (`drafts.exact`) read 32-bit code only: a 64-bit game gets Ghidra's drafts. PDBs and maps of 64-bit games
  are read as 32-bit ones are, untested on a real game's. A whole-program optimised game's cold parts of functions,
  which `.pdata` chains to a function elsewhere, stay functions of their own.

### PSP: encrypted programs, ZSO images, imported names
Most retail PSP programs (EBOOT.BIN) are encrypted, and decrypting them takes Sony's keys: decompdecomp doesn't
carry them, so the user imports a decrypted copy (PPSSPP can save one). A pinned, checksummed decrypter installed
from Setup, like a compiler, would let a UMD image import as it is, and its encrypted modules with it (left out
now). Also: a module's own $gp isn't the judge's (one per project: a module that keeps small data where the main
program doesn't gets its own flags, but its $gp-relative data is named from the main program's $gp), its imported
variables (.rodata.sceVstub) aren't named, ZSO images (LZ4) aren't read, SN's pspsnc is tested on the demo only (no
SN-built game was at hand: the readable one is CodeWarrior's, the others encrypted), and the 2.4.1 CodeWarrior some
games were built with isn’t archived (3.0.1 matches small functions; bigger ones unverified).

### PSP drafts: the Allegrex's own instructions
m2c has no Allegrex target: the EE's reads PSP code, and knows `seb`, `seh` and `madd` but not `min`, `max`, `ext` or
`ins`, which PSP GCC emits from plain C. Drafts have `M2C_ERROR(unknown instruction)` there, and those functions
rarely match from autopilot (the guide tells agents what each one is). Rewriting them for m2c before it reads the
code (`min` as `slt` and `movn`, outside delay slots) would give those drafts their arithmetic back.

### Windows distribution

The Windows release MSI bundles Python and the app, creates Start and desktop shortcuts, and updates
through the app. Next: sign the installer and check for WebView2 and WSL during setup.

### Better splits reach existing ROM projects

A ROM project (N64, PS2) is split once, when it's made: a better split (code splat misses, function ends, compiler
ranges) only reaches games added after it. DS projects get theirs at start (`NDS.ANALYSIS`,
`ForkEngine.reanalyse`): versioned, kept only when the rebuild is unchanged, and the project's activity says so.
ROM mode needs the same, with more to carry: a split version in each project, and on start a new split whose
functions take over the old ones' work (matches, attempts, notes, claims) by address, kept only when every matched
function still matches and the export still rebuilds the game byte for byte. Functions the new split cuts or joins
go back to the queue with their old attempts as notes. Each improvement to the split bumps the version.

### How much usage each agent has left

Named Claude accounts can be assigned to helpers, with separate recorded usage (#45). Remaining allowance is
still planned. A window that shows, for each agent you use, what's left of its limits: the 5-hour and weekly limits of Claude Code
and Codex, and a monthly limit or the credit left on an API key where the provider says it. Helpers already wait for a
usage limit to reset; this shows it before they hit it, so you can pick which agents to start.

### Big functions: measured, and in ROM projects

Parts, big-function briefs and part claims shipped (Recently done). Next: measure them on the DS game's 25 big open
functions with `big_function_report` (tried, compiled and how far, against the week before), and give ROM projects
parts (the gap's note under the table says what that needs).

### Library decomps built locally

Twins across your games already match library code you matched once. The next step: match library decomps and
libgcc/newlib, built on your computer with the project's compiler (libultra on N64, the PS2 SDK libraries), and try
them as twins. Library sources are fetched only when you ask, never bundled: SDK code is the console maker's
copyright.

### Names from another build you own

Many games have a second build with symbols: a demo, a prototype, or a port to another platform. C++ symbols say the
class and the parameter types, so carrying them over gives agents real signatures. Pair functions by the text and
constants they use, spread the pairs through the call graph, and let the user approve the names in bulk (as boundary
reports are approved now). Names only: matching is unaffected. It also names the classes `get_class` finds.

### C++ classes and struct layouts for PowerPC and ARM

The class recovery reads MIPS and x86 (Windows PC: RTTI's names, the classes of code built without it, and their
fields: done), and struct layouts read MIPS and x86 loads and stores. Left: GameCube and Wii (CodeWarrior: the vtable
pointer often isn't at offset 0) with a PowerPC reader, and DS with an ARM one, in fork mode: a class reader and the
processor's `Arch.field_accesses` (`arch/ppc.py`, `arch/arm.py`), which `classes.from_facts` and `structs.py` use as
x86's do (`arch/x86_fields.py`). GBA gains the ARM reader too. Until then their agents aren't offered `get_class` or
`guess_struct`.

### A modding-ready check for the export

The export rebuilds the game byte for byte; mods need code that can move. Build a second copy with padding inserted
and report every address that is still hard-coded ("93% movable, 41 addresses left"). The matching build stays as
it is. A Windows PC project's rebuild puts each function back at the game's address in the user's copy
(tools/rebuild.py): moving code there needs its data delinked too, and a linker that lays the program out.

### A failed build step counts against its unit in make-built forks too

In a fork built with ninja, a unit whose build fails at a step before its object (objects linked into one, a
generated source) counts as not building: `ninja -t query` says what each failed step goes into. A make-built fork
has no such query, so only the failed target itself is counted, and an edit that breaks such a step there can still
be kept as breaking nothing. Make's own dry run (`make -n -W <file>`) or its database (`make -p`) could say the same.

### A list of open rooms (to investigate)

Asked for by a user: a page where you see the rooms running now and join one. Hosts would still host their own
rooms, as now (the host's decompdecomp is the room, through its tunnel or address); a small directory server would
only list the public rooms whose hosts chose to list them: the game's name and hash, the platform, % matched, members,
and the room's address, dropped when the host stops answering. Joining is unchanged: the guest's own copy of the game
must have the host's SHA-1, and only the work travels.

Before building it, find out:

- **Is it allowed?** What a public directory of rooms means legally for the project, in the places it's used: what
  it may show (titles are trademarks: name only, no box art or screenshots), whether listing a room makes the
  directory responsible for what the room does, and what notice-and-takedown it needs. Check that nothing a listed
  room sends is the game's code or data, for git projects too (forkroom.py: what a joining guest gets of the host's
  tree).
- **Who runs it, and for what cost.** The site is static (GitHub Pages), so the list needs a host of its own; a
  small worker with a heartbeat per room may be enough.
- **Abuse and privacy.** Listing is opt-in per room and off by default; a room on a plain address shows the host's
  IP, so only tunnelled rooms could be listed; room names need limits, and a host or the directory can delist one.

The answer goes here: build it, build part of it, or not, and why.

## Later

- **Better look-alikes.** A second measure (edit distance over opcodes) next to today's, and control-flow-aware shared runs of
  instructions. Straight-line operand-aware fragments are available now; interleaved helper recovery remains future work.
- **Decoders for data formats.** Tools that decode N64 display lists, PS2 GS/VIF packets and GameCube GX command
  lists for agents.
- **The permuter's C, tidied.** A matching variant from decomp-permuter can be hard to read; a polishing helper
  could tidy it, still matching.
- **Polish in the drafts.** Methods of known classes are drafted as `Class::method` in the brief; the polishing pass
  doesn't use them yet.
- A Flatpak on the GNOME runtime.
- macOS.

### Revision patches and recipe previews in ROM mode

| Feature | N64 | PS1 | PS2 | GameCube · Wii | Windows PC | DS | Public decomps |
| :-- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Revision patches and bounded recipe previews | — | — | — | ✓ | ✓ | ✓ | ✓ |

Project-mode agents can use `function_brief`, `compile_patch` and `try_recipes` to avoid resending definitions
and evaluate up to eight named patch sets with the normal strict judge. ROM mode still needs a transaction over
its saved best source and context plus a preview path which never records or accepts an exact match. Until then
these tools are exposed only for source-tree projects; `compile_and_diff` remains available everywhere.


### Advisory code-block search

| Feature | N64 | PS1 | PS2 | GameCube · Wii | Windows PC | DS | Public decomps |
| :-- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Read-only straight-line code-block search and large-function hints | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

`find_code_blocks` finds bounded, operand-aware fragments in other target functions in both ROM and project modes.
Large-function briefs include up to three findings; autopilot warms a bounded set of large open functions. Matches,
source and claims remain untouched. Recovery across control-flow boundaries, instruction interleaving and changed
stack offsets remains future work. See [tool details and limits](docs/COMPILE_TOOLS.md#code-blocks-in-large-functions).


### Matched-function pattern library

| Feature | N64 | PS1 | PS2 | GameCube · Wii | Windows PC | DS | Public decomps |
| :-- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Matched donor patterns, scoped source observations and held-out retrieval diagnostics | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

The read-only library supplies compact donor hints in briefs and exposes `function_patterns` and
`pattern_index_report` to both agent interfaces. Semantic family labels, recovered shared struct types, compiler
rule induction and measurements of actual decompilation success remain future work; observed spellings and opcode
similarity alone cannot establish them. See [limits and evaluation](docs/COMPILE_TOOLS.md#matched-function-pattern-library).


### Tiny-function templates

| Feature | N64 | PS1 | PS2 | GameCube · Wii | Windows PC | DS | Public decomps |
| :-- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Tiny-function solver through the strict judge | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Autopilot and `solve_tiny_functions` use explicit simple signatures and actual target instructions. Portable empty
void returns are supported; constant/trap/direct-tail templates currently target x86. Other architecture templates,
complex signatures and ABI inference for undeclared wrappers remain planned work. No template bypasses validation.

- Done: bounded near-match finishing for source-tree projects, with exact-only preview/apply, claim protection,
  matched-neighbor guards and autopilot integration. ROM-mode finishing remains planned; raw-address target
  migrations, local-table equivalence and control-flow source repairs still require review.

### Persistent experiment retrieval and retry ordering

| Feature | N64 | PS1 | PS2 | GameCube · Wii | Windows PC | DS | Public decomps |
| :-- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Persistent attempt history with bounded source changes | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Fewer-attempt tie-breaker for automatic easiest/finish claims | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

`experiment_history` exposes recorded attempts to both agent interfaces across restarts without recompiling.
It labels equal scores and source identity conservatively; generated-code fingerprints include code, relocations and local data; full build-state identity is not available. Automatic claims prefer fewer attempts within existing priority groups;
explicit claims and closest/hardest/random strategies keep their behavior. Measure strict matches, matched
bytes and time/tokens per match on a fixed cohort before claiming a throughput improvement. Named bounded recipe previews now share outcomes, timings, revisions and output fingerprints across helpers.
Persisted full diffs and complete-state verdict caching remain planned.


### Scoped specialist support and independent target checks

Implemented one source owner with expiring, revision-scoped blocker requests to existing helpers. Specialists
provide bounded read-only advice; owners alone compile and accept. Shared preview history prevents blind repeats.
ELF x86 block inspection exposes CFG, conservative register/stack flow and localized current-target mismatches.
Unsupported formats explicitly abstain from dataflow. Existing tiny templates now cover typed integer/pointer
identities and zero-offset loads; bounded compiler recipes target observed scheduling and integer-copy patterns.

Original ELF branch/data destinations, instruction/function spans, relocation operands and unmasked bytes are
validated independently. Declared supported evidence stays mandatory across restarts, target mutations invalidate
comparisons, and fresh whole-unit baselines protect matched neighbors. These checks add baseline build cost.
Synthetic coverage measurements are available through tests/benchmark_solver_extensions.py; real cohort throughput
and tokens per accepted byte still require a separate run before any live rollout.

### Durable compiler operations and region experiments

PS4 source files can now be divided at function boundaries through `split_file`, inheriting the original
compiler/flags and preserving original target records. The existing split transaction protects exact neighbors;
an isolated function can then be tested with its own compiler. Defined-function PLT tail jumps are bound directly
only when original owner/destination/opcode evidence agrees. Canonical TLSGD detection remains separate from
explicit offset-plus-FS-base accesses.

| Feature | N64 | PS1 | PS2 | GameCube · Wii | Windows PC | DS | Public decomps |
| :-- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Durable request IDs, region evidence and scoped recipe proposals | — | — | — | ✓ | ✓ | ✓ | ✓ |
| Isolated compiler IR and optimization remarks | — | — | — | — | — | — | configured PS4 Clang adapter |

Project-mode compiles can be submitted once and polled after transport timeouts. Interrupted operations never
replay automatically. Revision-bound regional experiments preserve failed hypotheses and compare block-row
changes while keeping the full strict judge. Integer-model boundary checks are advisory models, not execution of
recovered code. Recipe proposals retain explicit prerequisites, build scope and recorded donor evidence; each use
still needs fresh judging. See [tool details](docs/COMPILE_TOOLS.md#durable-compile-operations-and-region-experiments).

Remaining work: ROM-mode operation/region adapters using its context and link-preview transactions; diagnostic
compiler adapters for other consoles/builds; finer queue/build/validation/diff timing and controlled efficiency
measurements; runtime differential checks with explicit relocation/callee/memory models. Never use historical
results, regional scores or supplied behavior models to approve a match.

PS4 call matching: targets now use per-function sections, preserve original
addresses separately, and normalize PLT32 calls without changing their identity.
Existing project tool copies require an explicit verified refresh.

Ninja project judging now follows failed intermediate build steps. Equivalent
dependency tracing for Make-built projects remains planned.
