"""The tools a decompiling agent gets, served over MCP to CLI agents and called directly by the
built-in API agent. Every tool works on the project the agent was started on."""

from __future__ import annotations

import re
from importlib import resources
from typing import Callable

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import __version__, agents, arch, consoles
from ..network import CLAIM_ORDERS, Agent, NetworkError
from . import (
    block_inspection,
    code_blocks,
    compile_operations,
    compile_tools,
    finishing,
    function_parts,
    function_patterns,
    region_work,
    scoped_recipes,
    specialists,
    tiny_functions,
    unit_compilers,
)
from .engine import RomNetwork

# The guides every console shares (app/guide/); each console's own are in its package (consoles/<id>/guides/)
GUIDES = {
    "workflow": "the matching loop, reading diffs, priorities",
    "mips": "MIPS registers, calling convention, delay slots, types from instructions",
    "m2c": "reading the machine-generated draft",
    "data": "globals, strings, tables, floats, data you define",
    "structs": "working out struct layouts from offsets",
    "naming": "naming functions and data",
    "fork": "working in a fork of a public decomp: the loop, conventions, what's allowed",
    "objdiff": "reading objdiff's diffs and match percentages",
    "ppc": "PowerPC (GameCube, Wii): registers, calling convention, common patterns",
    "cpp": "decompiling C++: classes, vtables, constructors, inlines, name mangling",
    "arm": "ARM and Thumb (GBA, DS, 3DS): registers, conditional code, literal pools",
    "aarch64": "AArch64 and Clang (Switch): registers, matching Clang output",
    "similarity": "finding similar functions and reusing matched code",
}


INSTRUCTIONS = """\
decompdecomp: reconstruct the user's game as C/C++ that passes its strict matching judge.
ROM projects compile and link at the original address; source-tree projects build their configured units and use
objdiff, including relocation and constant checks. Only the tool's matched verdict establishes a match.
Each claimed definition has one source owner. An advisory specialist inspects one named blocker and reports
proposed recipes; only the owner compiles and integrates. Read shared experiment_history before repeating work.
Use inspect_blocks for unresolved large-function regions and request_specialist only with concrete evidence.
Source-tree projects: a big function's C can be sent one part at a time (list_parts, compile_part); several agents
can hold parts of one function (claim_part), each judged on the whole function.
For source-tree compiles prefer compile_start, then poll compile_status. Reuse the same request_id after transport
failure; never submit a new mutation merely because a client timed out. Wait for a terminal state before ending
your session or releasing the claim. Status waits up to 5 seconds by default to avoid rapid polling.
For large functions read region_history,
record one evidenced region/hypothesis, and attach its region_id to compile_start. Review compare_region after it
finishes. action="diagnostics" explains the configured compiler in isolation where supported. Recorded recipes
remain scoped proposals: check prerequisites and judge every application. Integer-model checks are advisory.
Use the project mode and instructions supplied in your work prompt. Claim functions, inspect evidence, compile,
and iterate through the decomp tools. Never publish game code or weaken the judge.
"""

WORK_PROMPT = """\
You are a decompilation agent in decompdecomp, working on {title}. Other agents work concurrently; respect claims.
Write C that compiles and links to exactly the original ROM bytes. Use only the decomp tools.
Do {count} claims you compiled (a batch is one; one you released without compiling it doesn't count), then stop.
Don't stop early or offer more claims "on request": nobody answers during a session; continue claiming.

1. claim_function(count=4). Start from the best saved attempt and notes; do not replace better work with a skeleton.
   Inspect the actual target, signature, calls and data. Use get_context for missing declarations. The machine draft is a hypothesis: verify its types and control flow. If no better saved
   attempt exists, start from it and fix those. redraft(function, context) can use recovered types/prototypes.
2. Use donor hints already in the brief. Request find_similar, function_patterns or find_code_blocks only when a
   relevant donor or region is missing. Similarity does not establish equivalent semantics or a byte match.
3. Compile the complete definition with compile_and_diff; a batch uses function="a, b" and both definitions.
   Diagnose the first substantive difference: destinations/constants, control flow, types/widths, expression shape,
   then scheduling/registers. Change one hypothesis at a time. For large functions, focus on one failing region
   while judging the whole function. Preserve working regions; do not repeat experiments disproved in the notes.
4. Trust the explicit matched verdict. Share verified prototypes/structs with edit_context and confirmed names
   with rename_symbol. Share narrowly scoped evidence, including exceptions; one match is not a global rule.
5. Follow the tool's stall budget ({budget}). Use remaining attempts for specific hypotheses, not cosmetic changes
   to consume a quota. At the limit, release_function with best result, exact remaining differences, tried changes
   and outcomes, and the next useful experiment. Use release_function(needs=...) only for a verified dependency. Use not_c=true only with
   positive evidence of hand-written assembly, never because a function is large or a few attempts failed.

The {compiler_guide} and workflow guides are available with guide(). Bulk tiny-function solving and near-match finishing belong to
autopilot; pattern_index_report is a maintenance diagnostic, not a step in solving each claim.
Never fetch or copy code from outside this project (another repository, a finished decomp, the internet).
Pure C, no inline assembly. Keep messages brief and work through tools.
"""

# What a ROM helper is told when its processor has the reader (never a tool the game's console doesn't offer)
WORK_STRUCTS = """\
The brief says when a function is handed a struct: guess_struct(function, "arg0") shows one as C."""
WORK_BOUNDARY = """\
The function runs on past its end into padding, linker leftovers or data? report_boundary(...) where it really
ends, then work on as it is."""

# A helper started on "make matched code readable" (options task=readable), in any project (app/readable.py);
# {names}: step 3, as the project's mode has it (READABLE_ROM, READABLE_FORK)
READABLE_PROMPT = """\
You are a decompilation agent in decompdecomp, working on {title}. The functions you get already match the game
byte for byte. Your job: make their C readable for people who will port or mod the game, without changing what
the compiler makes of it. Other agents may work on other functions at the same time. Repeat for up to {count}
functions, then stop:

1. claim_readable() gives you the next matched function that isn't readable yet, with its matching C.
2. Read the function, and get_function on the functions it calls, until you understand what it does.
3. {names}
4. make_readable(function, source): the same function with meaningful parameter and local names, the header's
   types and names, and one comment saying what it does. Keep the statements' shape (order, casts, temporaries):
   it's judged again and must still match byte for byte, or it's refused and the matching C stays. Refused? Undo
   your last change and try a smaller one.

Once every function in a source file is readable, claim_readable may give you the file to tidy instead (TIDY A
FILE): follow its list with edit_file and rename_symbol, then tidy_done. A file counts as one of your {count}.

Write C people would write. Be brief in your messages: work, don't narrate.
"""

READABLE_ROM = ("rename_symbol the function, and the globals and functions it uses, once their purpose is clear.\n"
                "   Describe structs, enums and constants in the shared header (get_context shows it) with\n"
                "   edit_context, and use those names.")
READABLE_FORK = ("rename_symbol the function, and the globals and functions it uses, once their purpose is clear\n"
                 "   (the project's config and every file change together). Describe structs, enums and constants in\n"
                 "   the project's headers with edit_file (read_file and search_code show where), and use those names.")

# FORK_PROMPT's {what}: a public decomp, or a project decompdecomp started from the user's copy. Told it was "a
# public matching-decompilation project", a model on a new DS project spent its first 7 minutes looking for the
# project's conventions in config/ and build.ninja; on an N64 game one went to fetch the finished public decomp
FORK_PUBLIC = "a public matching-decompilation project for {platform}"
FORK_SCRATCH = ("a decompilation of a {platform} game that decompdecomp started from the user's own copy. It's new: "
                "there's no upstream project, and no existing code or conventions to find beyond what agents wrote. "
                "The project's config/, build files and tools are the build's, not yours: each function's brief has "
                "what you need, so claim and compile right away")

FORK_PROMPT = """\
You are a decompilation agent in decompdecomp, continuing {title}: {what}.
Other agents work concurrently; respect claims. Use only the decomp tools to write C/C++ that passes the project's
strict judge. Do {count} claims you compiled (a batch is one; one you released without compiling it doesn't count), then stop.
Don't stop early or offer more claims "on request": nobody answers during a session; continue claiming.

1. claim_function(count=4). Start from the best saved attempt and notes. Preserve better existing work; do not
   replace it with a skeleton. Verify target destinations, signature and actual instructions, not size estimates.
2. You own the claimed definition. A function of 400+ instructions is worked a part at a time: list_parts shows
   its parts; mark them in the C (/* part N */ ... /* end part N */, comments only) and send one part's C at a time
   with compile_part. If it has no C yet, compile_and_diff a skeleton (prologue, switch/branches with empty parts,
   epilogue) first. A big function someone else holds may have free parts: claim_part takes one.
   For a large function use inspect_blocks to select the unresolved region.
   Request a specialist only for a concrete blocker with instruction/block evidence, after checking shared history.
   Keep the claim; the specialist may inspect/propose but cannot compile or edit. Use specialist_status to retrieve
   findings and run proposed edits through try_recipes/compile_patch yourself; refresh stale revisions first.
   Before repeating work on a previously attempted function, read experiment_history for tried source changes.
   Historical scores do not prove current matches or equal assembly. Read only missing context: relevant source/header regions and existing declarations. Reuse types and helpers.
   Use donor hints already in the brief. Request find_similar, function_patterns or find_code_blocks only for a
   missing donor or region. Inspect donor C; similarity and recorded matches are leads, not current verification.
3. For an existing definition, function_brief returns its source and revision. Prefer compile_start(function,
   request_id, revision, edits=[{{old,new}}]) for unique replacements, then poll compile_status until terminal.
   Keep the same request_id/arguments after a transport timeout. Refresh stale revisions with a NEW request_id;
   never overwrite newer work. Use action="definition" and source for a complete rewrite or absent definition
   (empty revision only when absent). Small synchronous batches still use compile_and_diff(function="a, b").
   Keep your claim and wait for the operation before releasing/ending the session. last_diff has omitted rows.
4. Diagnose the first substantive mismatch: destinations/constants, control flow, types/widths, expression shape,
   then scheduling/registers. State a short hypothesis and predicted instruction change; change one cause per
   compile. For a large function, work on one failing region while validating the whole function. Preserve working
   regions. Read region_history and record_region with the current revision, target offsets, hypothesis/evidence;
   attach its region_id to compile_start and review compare_region after it finishes. Record negative/recovered
   findings even if the whole-function score stays flat. Never infer correctness from percentage alone.
   action="diagnostics" gives isolated compiler IR/remarks where supported. recipe_candidates gives scoped proposals;
   check prerequisites. Integer-model boundary checks supplement evidence but do not execute or prove the C.
   Use try_recipes for a small relevant set of alternatives, not a blind sweep. Previews need apply=true to keep a
   winner. Local btver2/no-sse recipes are experiments; inspect full-function ISA/ABI requirements first.
5. Trust the explicit matched verdict, including relocation/constants and linked-file checks. Shared types,
   declarations and helpers belong in edit_file, never inside compile_and_diff's source; function-local experiments
   belong in compile_patch. Existing
   matches must survive. When the tool reports a completed file, inspect file_status and fix its data as directed.
6. Follow the tool's stall budget ({budget}). Use attempts for specific hypotheses, not quota-filling changes.
   At the limit, release with the best result, exact remaining differences, tried changes and outcomes, and next
   experiment. Use release_function(needs=...) only for a verified dependency. Large functions and failed guesses do not justify not_c;
   use it only with positive evidence of hand-written assembly. Report real boundary problems through the tools.

Share conventions with donor evidence, scope and exceptions; do not present a single successful spelling as a
project-wide rule. Bulk tiny-function solving belongs to autopilot. pattern_index_report is maintenance work,
not a per-claim step. Read detailed guides only when relevant: {guides}.
C/C++ only; no inline asm, asm functions or asm includes. Preserve repaired symbol-linkage aliases already in the
source. Never edit targets, configs or build scripts. Never fetch or copy code from outside this project
(another repository, a finished decomp, the internet). Keep messages brief and work through tools.
"""

# What a fork whose console can join functions is told (the base prompt names no tool its console lacks)
FORK_JOIN = """\
Part of another function the analysis cut (no prologue, registers the code before set, a switch's cases in the
next symbol)? join_functions(it, the other part, why), so it becomes one whole function."""

# What a ROM helper whose engine can join functions is told (the base prompt names no tool its engine lacks)
ROM_JOIN = """\
Part of another function the analysis cut (no prologue, registers the code before set, a switch's cases in the
next symbol)? join_functions(it, the other part, why), so it becomes one whole function."""


def _console_guides() -> dict:
    """topic -> file of the guides consoles keep in their packages (consoles/<id>/guides/<topic>.md)."""
    return {path.stem: path for folder in consoles.guide_dirs() for path in sorted(folder.glob("*.md"))}


def guide_text(topic: str = "", platform: str | None = None) -> str:
    """A guide's text. With `platform` (a project's), the parts its file marks for other platforms are left out
    (for_platform)."""
    topic = topic.strip().lower()
    own = _console_guides()
    if topic in own:
        return for_platform(own[topic].read_text(encoding="utf-8"), platform)
    if topic not in GUIDES:
        # a console's guide is described by its title, the first line of its file
        about = GUIDES | {k: p.read_text(encoding="utf-8").split("\n", 1)[0].lstrip("# ") for k, p in own.items()}
        listing = "\n".join(f"- {k}: {v}" for k, v in about.items())
        return f"Guides (call guide with one of these topics):\n{listing}"
    return for_platform(resources.files("decompnet.app").joinpath("guide", f"{topic}.md").read_text(encoding="utf-8"),
                        platform)


# A guide's lines for some platforms only: `<!-- only: ppc mips -->` up to `<!-- end -->`; `<!-- only: !x86 -->` is
# every platform but those. Words are processor ids (arch/) or console ids. A PC helper's prompt (14 KB) carried
# GameCube's split_file, func_8001234 names and s32 types; a GameCube one would carry x86's conventions.
_ONLY = re.compile(r"^<!--\s*only:\s*([^>]*?)\s*-->\s*\n(.*?)^<!--\s*end\s*-->\s*\n", re.M | re.S)


def for_platform(text: str, platform: str | None) -> str:
    """`text` with its `only:` blocks kept for `platform` and dropped for others; None keeps every block (the
    markers themselves always go)."""
    con = consoles.get(platform) if platform else None
    ids = {con.id, con.arch} - {""} if con else set()

    def block(m: re.Match) -> str:
        if con is None:
            return m.group(2)
        words = m.group(1).split()
        yes = [w for w in words if not w.startswith("!")]
        no = {w[1:] for w in words if w.startswith("!")}
        keep = (not yes or bool(ids & set(yes))) and not (ids & no)
        return m.group(2) if keep else ""
    return _ONLY.sub(block, text)


def up_front(platform: str | None, kind: str, toolchain: str) -> str:
    """The know-how a helper gets before its first claim, for its project's platform: the loop's guide (a fork's
    and objdiff's, or ROM projects' workflow) and its console's compiler guide."""
    guide = consoles.get(platform).up_front_guide(toolchain)
    # Consoles can opt into a maintained quick reference; historical recipes stay available on demand.
    if guide:
        full = guide_text(guide, platform)
        quick = re.search(r"(?ms)^## Agent quick reference\n(.*?)(?=^## |\Z)", full)
        if quick:
            return ("Current platform quick reference (details: guide(\""+guide+"\")):\n"+quick[1].strip()
                    + '\nDiff categories: missing/extra instructions, operands/constants/destinations, order, registers, stack. '
                    'Fix substantive differences before allocation. The explicit matched verdict is authoritative. '
                    'Read guide("objdiff") or guide("workflow") when the comparison format is unclear.')
    topics = ["fork", "objdiff"] if kind == "fork" else ["workflow"]
    topics += [guide] if guide else []
    return "\n\n".join(guide_text(t, platform) for t in topics)


def _fork(net: RomNetwork, slug: str, what: str) -> str:
    if not net.fork_slug(slug):
        raise NetworkError(f"{what} is for forks of public decomps; in a ROM project the shared header is get_context")
    return slug


def project_info(net: RomNetwork, slug: str) -> str:
    wish = net.instructions(slug)
    text = _project_info(net, slug)
    open_ = net.overview(slug).get("suggestions") or []
    if open_:  # so agents agree with one (suggest the same) rather than repeat it in other words
        text += "\nOpen suggestions to the user (suggest the same compiler and flags to agree):\n" + "\n".join(
            f"- {s['toolchain'] or '(setup)'} {' '.join(s['flags'])}"
            + "".join(f" 0x{a:X}-0x{b:X}" for a, b in s.get("ranges", [])) + f": {s['text'][:200]} [{len(s['agents'])} agree"
            + (f"; {s['check']}" if s["check"] else "") + "]" for s in open_)
    if not net.fork_slug(slug) and (known := net.knowledge.for_prompt(slug)):
        text += "\n" + known + "\n"
    if (order := net.claim_order(slug)):
        text += (f"\nThe project's owner chose the order helpers take functions in: {CLAIM_ORDERS[order]}. "
                 "claim_function without a function follows it; name a function to get that one.\n")
    return text + (f"\nThe user's instructions for this project (they come first):\n{wish}" if wish else "")


def _project_info(net: RomNetwork, slug: str) -> str:
    if net.fork_slug(slug):
        return net.forks.info(slug)
    ov = net.overview(slug)
    pct = 100 * ov["matched_bytes"] / (ov["bytes"] or 1)
    ranges = "".join(f"\n  0x{r['start']:X}-0x{r['end']:X}: {r['name']} {' '.join(r['flags'])}" for r in ov["compiler_ranges"])
    return (f"{ov['title']} ({ov['rom']['game_code']}, {ov['rom']['region']})\n"
            f"compiler: {ov['compiler']['name']} {' '.join(ov['compiler']['flags'])}"
            + (f"; these address ranges are built with another:{ranges}" if ranges else "") + "\n"
            + (f"this project's compilers (for try_compiler): {', '.join(ov['compilers'])}\n"
               if len(ov["compilers"]) > 1 else "")
            + f"progress: {ov['matched']}/{ov['functions']} functions matched ({pct:.1f}% of code), "
            f"{ov['attempts']} compiles so far, context.h v{ov['context_version']}\n"
            f"Next: claim_function(). Guides: guide().")


# name -> handler(net, agent, slug, **args); every handler returns text for the model
def _claim(net: RomNetwork, agent: Agent, slug: str, function: str, strategy: str, count: int = 1) -> str:
    """A claim; one asked for that's matched, set aside (no C makes it) or someone else's is the next free function
    instead (an agent asking for those has nothing better to do than the next one, and is told why). count > 1: a
    batch of small ones."""
    strategy = strategy or ("finish" if net.fork_slug(slug) else "easiest")
    if not function and int(count or 1) > 1:
        return net.claim_many(agent, slug, int(count), strategy)
    try:
        return net.claim(agent, slug, function or None, strategy)
    except NetworkError as e:
        if not function or not re.search(r"already matched|is set aside|is claimed by", str(e)):
            raise
        if "is claimed by" in str(e) and net.fork_slug(slug) and (parts := function_parts.offer(net, slug, function)):
            return f"({e}.) {parts}"  # a big function in parts: there's work on it for this agent too
        return f"({e}. Here's the next free function instead.)\n\n" + net.claim(agent, slug, None, strategy)


def _compile(net: RomNetwork, agent: Agent, slug: str, function: str, source: str) -> str:
    """compile_and_diff; several names ("a, b, c") judge several definitions sent together."""
    names = [n.strip() for n in function.split(",") if n.strip()]
    if len(names) <= 1:
        return net.compile_and_diff(agent, names[0] if names else function, source, slug)
    return net.attempt_many(agent, [net.resolve_function(agent, slug, n) for n in names], source, slug)


def _add_note(net: RomNetwork, agent: Agent, slug: str, text: str, function: str = "") -> str:
    """A note for the next agent on one function. #25: a note without one used to become a project-wide knowledge
    proposal, which read like agreed knowledge although one model had only guessed it; that's share_knowledge's
    job when an agent means it."""
    if not function.strip():
        raise NetworkError("add_note needs the function the note is for; something every agent on this game should "
                           "know goes through share_knowledge")
    return net.add_note(agent, slug, text, function)


HANDLERS: dict[str, Callable[..., str]] = {
    "compile_start": lambda net, agent, slug, **args:
        compile_operations.start(net, agent, _fork(net, slug, "compile_start"), **args),
    "compile_status": lambda net, agent, slug, operation_id, wait_ms=5000:
        compile_operations.status(net, agent, slug, operation_id, wait_ms),
    "record_region": lambda net, agent, slug, **args:
        region_work.record(net, agent, _fork(net, slug, "record_region"), **args),
    "region_history": lambda net, agent, slug, function, limit=6, before=0:
        region_work.history(net, slug, function, limit, before),
    "compare_region": lambda net, agent, slug, function, region_id:
        region_work.comparison(net, agent, slug, function, region_id),
    "check_integer_behavior": lambda net, agent, slug, **args:
        region_work.check(net, agent, _fork(net, slug, "check_integer_behavior"), **args),
    "save_recipe": lambda net, agent, slug, **args:
        scoped_recipes.save(net, agent, _fork(net, slug, "save_recipe"), **args),
    "recipe_candidates": lambda net, agent, slug, function, limit=5:
        scoped_recipes.candidates(net, _fork(net, slug, "recipe_candidates"), function, limit),
    "project_info": lambda net, agent, slug: project_info(net, slug),
    "claim_function": lambda net, agent, slug, function="", strategy="", count=1:
        _claim(net, agent, slug, function, strategy, count),
    "get_function": lambda net, agent, slug, function: net.get_function(function, slug, agent),
    "function_brief": lambda net, agent, slug, function:
        compile_tools.brief(net, agent, _fork(net, slug, "function_brief"), function),
    "compile_patch": lambda net, agent, slug, function, revision, edits:
        compile_tools.compile_patch(net, agent, _fork(net, slug, "compile_patch"), function, revision, edits),
    "try_recipes": lambda net, agent, slug, function, revision, recipes, apply=False:
        compile_tools.try_recipes(net, agent, _fork(net, slug, "try_recipes"), function, revision, recipes, apply),
    "compile_and_diff": lambda net, agent, slug, function, source: _compile(net, agent, slug, function, source),
    "inspect_blocks": lambda net, agent, slug, function, offset=None, start=0, limit=12, instruction_limit=80:
        block_inspection.inspect_blocks(net, agent, slug, function, offset, start, limit, instruction_limit),
    "request_specialist": lambda net, agent, slug, function, category, question, evidence:
        specialists.request(net, agent, slug, function, category, question, evidence),
    "specialist_status": lambda net, agent, slug, function, request_id=0, cancel=False:
        specialists.status(net, agent, slug, function, request_id, cancel),
    "specialist_brief": lambda net, agent, slug, request_id:
        specialists.brief(net, agent, slug, request_id),
    "report_specialist": lambda net, agent, slug, request_id, findings, proposals=None:
        specialists.report(net, agent, slug, request_id, findings, proposals or []),
    "suggest_recipes": lambda net, agent, slug, function:
        compile_tools.suggest_recipes(net, agent, _fork(net, slug, "suggest_recipes"), function),
    "experiment_history": lambda net, agent, slug, function, limit=6, before=0:
        net.experiment_history(net.resolve_function(agent, slug, function), slug, limit, before),
    "last_diff": lambda net, agent, slug, function, context=2: net.last_diff(agent, function, slug, context),
    "release_function": lambda net, agent, slug, function, notes, needs="", not_c=False:
        net.release_function(agent, function, notes, slug, needs or "", bool(not_c)),
    "get_context": lambda net, agent, slug, function="": net.get_context(slug, function or None),
    "edit_context": lambda net, agent, slug, old, new, message, function="":
        net.edit_context(agent, slug, old, new, message, function or None),
    "rename_symbol": lambda net, agent, slug, old, new, message="": net.rename_symbol(agent, slug, old, new, message),
    "split_function": lambda net, agent, slug, function, at, new_name="":
        net.split_function(agent, slug, function, at, new_name),
    "report_boundary": lambda net, agent, slug, function, end, reason:
        net.request_boundary(agent, slug, function, end, reason),
    "read_data": lambda net, agent, slug, what, length=64: (net.forks.read_data(agent, slug, what, length)
                                                            if net.fork_slug(slug) else net.read_data(slug, what, length)),
    "get_class": lambda net, agent, slug, what="": net.get_class(slug, what),
    "guess_struct": lambda net, agent, slug, function, argument="arg0": net.guess_struct(slug, function, argument),
    "redraft": lambda net, agent, slug, function, context: net.redraft(slug, function, context),
    "compiler_map": lambda net, agent, slug, survey=False, check_ranges=False:
        net.compiler_map_text(slug, bool(survey), bool(check_ranges)),
    "list_functions": lambda net, agent, slug, status="open", limit=40: net.list_functions(slug, status, limit),
    "add_note": lambda net, agent, slug, text, function="": _add_note(net, agent, slug, text, function),
    "share_knowledge": lambda net, agent, slug, text="", supports=0: net.knowledge.share(agent, slug, text, supports),
    "search_flags": lambda net, agent, slug, function, compiler="", source="":
        net.search_flags(agent, slug, function, compiler, source),
    "claim_readable": lambda net, agent, slug: net.readability.claim(agent, slug),
    "make_readable": lambda net, agent, slug, function, source: net.readability.submit(agent, slug, function, source),
    "tidy_done": lambda net, agent, slug, unit, summary: net.readability.tidy_done(agent, slug, unit, summary),
    "suggest": lambda net, agent, slug, text, compiler="", flags="", function="", ranges="", source="":
        net.suggest(agent, slug, text, compiler, flags, function or None, ranges, source),
    "try_compiler": lambda net, agent, slug, function, compiler, flags="", source="":
        net.try_compiler(agent, slug, function, compiler, flags, source),
    "search_notes": lambda net, agent, slug, query: net.search_notes(slug, query),
    "finish_near_matches": lambda net, agent, slug, limit=50, apply=False:
        finishing.run(net, agent, net._project(slug), limit, apply),
    "solve_tiny_functions": lambda net, agent, slug, limit=100, apply=False:
        tiny_functions.solve_tiny_functions(net, agent, slug, limit, apply),
    "function_patterns": lambda net, agent, slug, function, limit=5:
        function_patterns.function_patterns(net, slug, function, limit),
    "pattern_index_report": lambda net, agent, slug, sample=10:
        function_patterns.pattern_index_report(net, slug, sample),
    "find_code_blocks": lambda net, agent, slug, function, limit=5:
        code_blocks.find_code_blocks(net, slug, function, limit),
    "find_similar": lambda net, agent, slug, function, limit=5: net.find_similar(slug, function, limit),
    "list_parts": lambda net, agent, slug, function, part=0:
        function_parts.list_parts(net, agent, _fork(net, slug, "list_parts"), function, int(part or 0)),
    "claim_part": lambda net, agent, slug, function, part=0:
        function_parts.claim_part(net, agent, _fork(net, slug, "claim_part"), function, int(part or 0)),
    "compile_part": lambda net, agent, slug, function, part, code:
        function_parts.compile_part(net, agent, _fork(net, slug, "compile_part"), function, part, code),
    "release_part": lambda net, agent, slug, function, notes="":
        function_parts.release_part(net, agent, _fork(net, slug, "release_part"), function, notes),
    "big_function_report": lambda net, agent, slug: function_parts.report(net, slug),
    "compiler_survey": lambda net, agent, slug, apply=False, limit=0:
        unit_compilers.survey(net, agent, _fork(net, slug, "compiler_survey"), bool(apply), int(limit or 0)),
    "guide": lambda net, agent, slug, topic="": guide_text(topic, net.overview(slug).get("platform")),
    "read_file": lambda net, agent, slug, path, start=1, count=200:
        net.forks.read_file(_fork(net, slug, "read_file"), path, start, count),
    "search_code": lambda net, agent, slug, pattern, glob="": net.forks.search(_fork(net, slug, "search_code"), pattern, glob),
    "edit_file": lambda net, agent, slug, path, old, new, message="":
        net.forks.edit_file(agent, _fork(net, slug, "edit_file"), path, old, new, message),
    "file_status": lambda net, agent, slug, path: net.forks.file_status(_fork(net, slug, "file_status"), path),
    "standins_for": lambda net, agent, slug, what: net.forks.standins_for(agent, _fork(net, slug, "standins_for"), what),
    "split_file": lambda net, agent, slug, path, at, new_name="", data="":
        net.forks.split_file(agent, _fork(net, slug, "split_file"), path, at, new_name, data),
    "join_functions": lambda net, agent, slug, function, with_function, reason:
        net.forks.propose_join(agent, _fork(net, slug, "join_functions"), function, with_function, reason),
}

READ_ONLY = frozenset({"project_info", "get_function", "last_diff", "get_context", "read_data", "get_class",
                       "guess_struct", "compiler_map", "list_functions", "search_flags", "search_notes",
                       "find_similar", "guide", "read_file", "search_code", "file_status", "standins_for"})

LTCG_ONLY = {"standins_for"}

ROM_ONLY = {"get_context", "edit_context", "split_function", "redraft", "search_flags"}
FORK_ONLY = {"tidy_done", "standins_for", "compile_start", "compile_status", "record_region", "region_history", "compare_region",
             "check_integer_behavior", "save_recipe", "recipe_candidates", "function_brief", "compile_patch", "try_recipes", "suggest_recipes", "read_file", "search_code", "edit_file", "file_status", "split_file", "join_functions",
             # parts replace C in the function's source file: a ROM candidate lives in none (function_parts.py)
             "list_parts", "claim_part", "compile_part", "release_part",
             # a file's compiler is in its source tree's build (Console.file_compiler): ROM projects have ranges
             "compiler_survey"}


def _rom_joins() -> bool:
    """Whether the ROM engine can join functions. #192 adds EditMixin.join_functions, which is enough on its own;
    an engine whose handler has another name sets RomNetwork.joins_functions."""
    return bool(getattr(RomNetwork, "joins_functions", False)) or callable(getattr(RomNetwork, "join_functions", None))


def _supports(console, name: str, fork: bool) -> bool:
    """Whether `console` can do `name` in this kind of project. A tool no console lacks keeps its place."""
    if name == "join_functions":
        # Project mode's joins are the console's (Console.join_functions); a ROM's are the engine's (#192).
        return console.joins_functions if fork else _rom_joins()
    if name == "report_boundary":
        return console.reports_boundaries if fork else arch.BOUNDARIES in console.features
    if name == "get_class":
        return console.reads_classes
    if name == "guess_struct":
        return console.reads_structs
    if name == "compiler_map":
        return console.file_compilers if fork else arch.COMPILER_MAP in console.features
    return True


def tool_names(fork: bool, console=None, ltcg: bool = False, scratch: bool = True) -> list[str]:
    """The tools that make sense for a project: forks and ROM projects share the core loop. A game started from
    scratch in project mode has those of ROM projects its console makes work there too (Console.own_tools: PC's
    redraft and search_flags). A tool its console can't do (a project mode's join or boundary, a processor without
    the reader for classes or structs, a compiler map), or a ROM join its engine can't make, isn't offered, so its
    prompt and schema don't either."""
    own = console.own_tools if console is not None and scratch else frozenset()
    skip = ROM_ONLY - own if fork else FORK_ONLY
    names = [n for n in HANDLERS if n not in skip and (ltcg or n not in LTCG_ONLY)]
    return [n for n in names if console is None or _supports(console, n, fork)]


def project_tools(net: RomNetwork, slug: str | None) -> list[str]:
    """tool_names for a project (None: no project yet), from the console its settings name."""
    if not slug:
        return tool_names(False)
    fork = bool(net.fork_slug(slug))
    cfg = net.cfg(net._project(slug))
    console = consoles.of(cfg)
    manifest = (net.forks.get(slug)[1].manifest or {}) if fork and cfg.get("origin") == "scratch" else {}
    return tool_names(fork, console if console.id or console.arch else None, bool(manifest.get("ltcg")),
                      scratch=cfg.get("origin") == "scratch")


def _named(net: RomNetwork, agent: Agent, slug: str, args: dict) -> dict:
    """Function names as agents write them (a plain symbol that repeats across files, symbol@unit, Class::method)
    turned into the stored name, preferring the function the agent holds."""
    function = args.get("function")
    if isinstance(function, str) and not function.strip():
        args = {**args, "function": ""}  # a blank name is no name: add_note says so, the other tools get none
    elif function and "," not in function:  # (several are resolved one by one, see _compile)
        args = {**args, "function": net.resolve_function(agent, slug, function)}
    return args


def _unoffered(net: RomNetwork, slug: str, name: str) -> str:
    """Why an old prompt or a cached tool list calling `name` is answered instead of run."""
    console = consoles.of(net.cfg(net._project(slug)))
    where = f"{console.name} projects" if console.name else "this project"
    subject = "its console"
    why = {"join_functions": "can't join functions yet", "report_boundary": "can't report function ends yet",
           "get_class": "can't read C++ classes yet", "guess_struct": "can't read struct layouts yet",
           "compiler_map": "can't read a compiler map yet"}.get(name)
    if name == "join_functions" and not net.fork_slug(slug):
        subject, why = "the ROM engine", "can't join functions yet"  # ROM joins are the engine's (#192), not a console's
    return (f"{name} isn't offered in {where}: " + (f"{subject} {why}" if why else "use the tools you were given")
            + " (ROADMAP.md says what's planned).")


def _invoke(net: RomNetwork, agent: Agent, slug: str, name: str, args: dict) -> str:
    """Every helper sees pulled changes before acting; only tools that write work need a second synchronization."""
    if name not in project_tools(net, slug):
        raise NetworkError(_unoffered(net, slug, name))
    net.progress.sync(slug, force=True)
    result = HANDLERS[name](net, agent, slug, **_named(net, agent, slug, args))
    if name not in READ_ONLY:
        net.progress.sync(slug, force=True)
    return result


def call(net: RomNetwork, agent: Agent, slug: str, name: str, args: dict) -> str:
    """Run one tool for the built-in agent; errors come back as text the model can act on."""
    if name not in HANDLERS:
        return f"error: unknown tool {name!r}"
    try:
        specialists.guard(net, agent, slug, name)
        compile_operations.guard(net, agent, slug, name)
        return _invoke(net, agent, slug, name, args)
    except NetworkError as e:
        return f"error: {e}"
    except TypeError as e:
        return f"error: bad arguments for {name}: {e}"


# What agents read on each tool's definition (Agent.tool_meta): Antigravity CLI otherwise finds MCP tools on demand,
# an extra step each, and decompdecomp's tools are all there is to the job, so they are loaded up front.
ALWAYS_LOAD = agents.TOOL_META


def build_mcp(net: RomNetwork, agent_of: Callable[[Context], tuple[Agent, str]]) -> MCPServer:
    """MCP server for CLI agents. `agent_of` maps a request to (agent, project slug)."""
    mcp = MCPServer(name="decomp", title="decompdecomp", instructions=INSTRUCTIONS, version=__version__)

    async def run(ctx: Context, name: str, **args) -> str:
        try:
            agent, slug = agent_of(ctx)
        except NetworkError as e:
            raise ToolError(str(e)) from None
        try:
            def invoke():
                specialists.guard(net, agent, slug, name)
                compile_operations.guard(net, agent, slug, name)
                return _invoke(net, agent, slug, name, args)
            return await anyio.to_thread.run_sync(invoke)
        except NetworkError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(meta=ALWAYS_LOAD)
    async def project_info(ctx: Context) -> str:
        """Which game you're working on, its compiler, and progress so far."""
        return await run(ctx, "project_info")

    @mcp.tool(meta=ALWAYS_LOAD)
    async def claim_function(ctx: Context, function: str = "", strategy: str = "", count: int = 1) -> str:
        """Claim the next function to decompile (or a specific one by name or address).

        Returns its assembly, the best previous attempt and notes from earlier agents, plus (ROM projects)
        calls, data and a machine-made draft or (forks) the current diff and its source file. Each compile
        renews the claim.

        Args:
            function: optional name or address (0x80001234) of a specific function
            strategy: finish (forks' default: the files closest to being linked first), easiest (fewest
                instructions; ROM projects' default), closest (nearly matched already), hardest, untried
                (functions nobody has compiled yet, then the smallest), random. If the project's owner
                chose an order helpers take first, claiming without a function follows theirs, whatever
                you pass here; a function you name still comes.
            count: up to 6: small functions come as a batch (neighbours in one file) to write together and
                judge in one compile_and_diff; a big one comes alone
        """
        return await run(ctx, "claim_function", function=function, strategy=strategy, count=count)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def get_function(ctx: Context, function: str) -> str:
        """Show any function (by name or address): assembly, status, best attempt, notes. Read matched
        functions to learn how this codebase is written."""
        return await run(ctx, "get_function", function=function)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def function_brief(ctx: Context, function: str) -> str:
        """Current definition once, score and revision for compile_patch/try_recipes; get_function has assembly/notes."""
        return await run(ctx, "function_brief", function=function)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compile_patch(ctx: Context, function: str, revision: str, edits: list[dict[str, str]]) -> str:
        """Apply exact {old,new} replacements in a saved definition, then run the normal strict judge.
        Get revision from function_brief. Stale revisions and ambiguous edits are rejected. Full diff: last_diff.
        """
        return await run(ctx, "compile_patch", function=function, revision=revision, edits=edits)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def try_recipes(ctx: Context, function: str, revision: str, recipes: list[dict], apply: bool = False) -> str:
        """Try up to eight {name,edits:[{old,new}]} recipes against one saved baseline. On x86, {name:"btver2"}
        or {name:"no-sse"} supplies a local target-attribute recipe. Preview restores even
        exact matches. apply=true keeps only a strictly better winner through the normal matched-neighbor guard.
        """
        return await run(ctx, "try_recipes", function=function, revision=revision, recipes=recipes, apply=apply)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compile_and_diff(ctx: Context, function: str, source: str) -> str:
        """Compile your code for `function` and diff it against the original; score 0 = byte-identical.

        ROM projects: compiled with the game's compiler (context.h included), linked at the function's
        address and compared with the ROM. Forks: send the complete definition (plus any #include "...",
        prototypes, externs or #pragma lines it needs); it replaces the function in its source file, which is
        built with the project's build system and diffed with objdiff. Several functions ("a, b, c"): send all
        their definitions in one source; each is judged on its own."""
        return await run(ctx, "compile_and_diff", function=function, source=source)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def list_parts(ctx: Context, function: str, part: int = 0) -> str:
        """A big function's parts: its target cut at blocks into 60-240 instructions each, which are marked in the C
        (/* part N */ ... /* end part N */) and who holds them. part=N: that part's assembly, its diff rows and the C
        with the other parts folded."""
        return await run(ctx, "list_parts", function=function, part=part)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def claim_part(ctx: Context, function: str, part: int = 0) -> str:
        """Take one marked part of a big function, beside whoever holds the function (no part: the free one with the
        most differing rows). Others can't change it while you hold it; compile_part renews it."""
        return await run(ctx, "claim_part", function=function, part=part)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compile_part(ctx: Context, function: str, part: int, code: str) -> str:
        """Replace the C between a part's markers with `code` and judge the whole function as compile_and_diff does;
        the answer is that part's diff rows and each part's count of differing rows. While others work on the
        function, a change that makes the score or another part's rows worse isn't kept."""
        return await run(ctx, "compile_part", function=function, part=part, code=code)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def release_part(ctx: Context, function: str, notes: str = "") -> str:
        """Give back your part of a function, with a note for the next agent on it."""
        return await run(ctx, "release_part", function=function, notes=notes)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compiler_survey(ctx: Context, apply: bool = False, limit: int = 0) -> str:
        """Build each source file with functions left to match with each other compiler of its console (outside the
        project's build): where enough of its functions match only with one and none is lost, report it; apply=true
        switches it, checked in the project, with its reason and evidence in docs/build-settings.md. A maintenance
        job (an hour or more on a big game): limit=N surveys the first N files."""
        return await run(ctx, "compiler_survey", apply=apply, limit=limit)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def big_function_report(ctx: Context) -> str:
        """How the functions of 400 instructions or more are doing: tried, compiled, how far, and compiles on them
        this week against last week."""
        return await run(ctx, "big_function_report")

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compile_start(ctx: Context, function: str, request_id: str, revision: str = "", action: str = "patch",
                            edits: list[dict] | None = None, source: str = "", recipes: list[dict] | None = None,
                            apply: bool = False, region_id: int = 0) -> str:
        """Start a durable patch/definition/recipes/diagnostics operation; returns immediately with its ID.
        Reuse the SAME request_id and identical arguments after a transport timeout. Poll compile_status;
        do not edit/release during an active operation. Source-tree projects only.
        """
        return await run(ctx, "compile_start", function=function, request_id=request_id, revision=revision,
                         action=action, edits=edits, source=source, recipes=recipes, apply=apply, region_id=region_id)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compile_status(ctx: Context, operation_id: str, wait_ms: int = 5000) -> str:
        """Read an existing operation; never replays a mutation or reuses its verdict for a changed build."""
        return await run(ctx, "compile_status", operation_id=operation_id, wait_ms=wait_ms)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def record_region(ctx: Context, function: str, revision: str, start: int, end: int, hypothesis: str,
                            evidence: str, outcome: str = "planned", findings: str = "") -> str:
        """Persist a concrete hypothesis at inclusive target instruction offsets, including failed/recovered work.
        Attach the returned region_id to compile_start to measure before/after block differences.
        """
        return await run(ctx, "record_region", function=function, revision=revision, start=start, end=end,
                         hypothesis=hypothesis, evidence=evidence, outcome=outcome, findings=findings)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def region_history(ctx: Context, function: str, limit: int = 6, before: int = 0) -> str:
        """Read region hypotheses, failures, linked operations and advisory comparisons across sessions."""
        return await run(ctx, "region_history", function=function, limit=limit, before=before)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compare_region(ctx: Context, function: str, region_id: int) -> str:
        """Read measured block-row changes inside/outside a regional trial; not a partial match verdict."""
        return await run(ctx, "compare_region", function=function, region_id=region_id)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def check_integer_behavior(ctx: Context, function: str, region_id: int, target_model: list,
                                     candidate_model: list, cases: list[dict]) -> str:
        """Compare supplied integer models on explicit boundaries; does NOT execute C or original code.
        Prefix arrays: add/sub/mul/and/or/xor/eq/lt/le, u8/u16/u32/u64 or i8/i16/i32/i64 casts,
        select(condition,yes,no). Store counterexamples with the region; passing never proves correctness.
        """
        return await run(ctx, "check_integer_behavior", function=function, region_id=region_id,
                         target_model=target_model, candidate_model=candidate_model, cases=cases)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def save_recipe(ctx: Context, name: str, prerequisites: str, evidence: str, donors: list[str],
                          edits: list[dict]) -> str:
        """Save a scoped recipe proposal with1–8 recorded matched donors and explicit prerequisites; no application."""
        return await run(ctx, "save_recipe", name=name, prerequisites=prerequisites, evidence=evidence,
                         donors=donors, edits=edits)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def recipe_candidates(ctx: Context, function: str, limit: int = 5) -> str:
        """Retrieve build-scoped proposals with current recorded donors and unique source edit strings.
        Owner checks remaining prerequisites, then tests through the strict judge with try_recipes.
        """
        return await run(ctx, "recipe_candidates", function=function, limit=limit)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def inspect_blocks(ctx: Context, function: str, offset: int | None = None, start: int = 0,
                             limit: int = 12, instruction_limit: int = 80) -> str:
        """List bounded target CFG blocks or select a byte offset for register/flag/stack facts and local diffs.
        Analysis is advisory, incomplete effects are labeled, and the full function remains the scoring unit.
        """
        return await run(ctx, "inspect_blocks", function=function, offset=offset, start=start, limit=limit,
                         instruction_limit=instruction_limit)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def request_specialist(ctx: Context, function: str, category: str, question: str, evidence: str) -> str:
        """Ask an existing idle helper one specific control_flow/layout/codegen/references blocker.
        Keep your claim: specialists can only inspect and propose recipes; the owner tests and integrates them.
        """
        return await run(ctx, "request_specialist", function=function, category=category, question=question, evidence=evidence)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def specialist_status(ctx: Context, function: str, request_id: int = 0, cancel: bool = False) -> str:
        """Read the scoped advisory findings/recipe proposals, or cancel your own outstanding request."""
        return await run(ctx, "specialist_status", function=function, request_id=request_id, cancel=cancel)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def specialist_brief(ctx: Context, request_id: int) -> str:
        """Assigned specialist's concrete question, evidence, revision and shared experiment history."""
        return await run(ctx, "specialist_brief", request_id=request_id)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def report_specialist(ctx: Context, request_id: int, findings: str, proposals: list[dict] | None = None) -> str:
        """Publish bounded advisory findings and up to four named exact-edit recipes; never apply them."""
        return await run(ctx, "report_specialist", request_id=request_id, findings=findings, proposals=proposals or [])

    @mcp.tool(meta=ALWAYS_LOAD)
    async def suggest_recipes(ctx: Context, function: str) -> str:
        """Read existing mismatch evidence for scoped compiler recipes; no compilation or flag changes."""
        return await run(ctx, "suggest_recipes", function=function)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def experiment_history(ctx: Context, function: str, limit: int = 6, before: int = 0) -> str:
        """Recorded experiments across agents and restarts, with bounded source changes and historical scores.
        Up to 20 entries; before is an exclusive attempt ID for older pages. Never reuses judge verdicts.
        """
        return await run(ctx, "experiment_history", function=function, limit=limit, before=before)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def last_diff(ctx: Context, function: str, context: int = 2) -> str:
        """Every differing row of your last compile_and_diff of `function`, where its result was cut ("more rows
        not shown"). Costs no compile and counts for nothing. `context`: equal rows shown around each difference."""
        return await run(ctx, "last_diff", function=function, context=context)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def release_function(ctx: Context, function: str, notes: str, needs: str = "", not_c: bool = False) -> str:
        """Give up a function you're stuck on, once compile_and_diff says to move on. Notes are required: what you
        tried, what is left. Your best attempt is kept for the next agent. Released before that (fewer compiles than
        the move-on rule), it answers once with what to try instead; call it again to release anyway.

        Args:
            function: the function you hold
            notes: what you tried, what is left
            needs: optional: another function of the game this one needs done first (a callee whose convention or
                body it depends on, a helper the original inlines). This one then waits, that one goes first in the
                queue, and you get it when it's free.
            not_c: true when you're sure no C makes its code at all (hand-written assembly), saying why in notes:
                once agents of two models agree, it's set aside and no one is handed it again.
        """
        return await run(ctx, "release_function", function=function, notes=notes, needs=needs, not_c=not_c)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def get_context(ctx: Context, function: str = "") -> str:
        """The shared header (types, structs, prototypes) included before every compile."""
        return await run(ctx, "get_context", function=function)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def edit_context(ctx: Context, old: str, new: str, message: str, function: str = "") -> str:
        """Edit the shared header: replace `old` (must occur exactly once) with `new`, or append `new` when
        old is "". Accepted only if every matched function still matches. Add prototypes and structs here."""
        return await run(ctx, "edit_context", old=old, new=new, message=message, function=function)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def rename_symbol(ctx: Context, old: str, new: str, message: str = "") -> str:
        """Give a function or data address a real name (e.g. func_80001234 -> player_update). Everything that
        uses the name changes with it (ROM: the assembly, matched sources, the header; a project: the game's
        symbols in its config and every source and header file); never breaks a match."""
        return await run(ctx, "rename_symbol", old=old, new=new, message=message)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def split_function(ctx: Context, function: str, at: str, new_name: str = "") -> str:
        """Split a function the disassembler merged with the next one (e.g. after a function that never
        returns). `at` is the address where the second function starts."""
        return await run(ctx, "split_function", function=function, at=at, new_name=new_name)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def report_boundary(ctx: Context, function: str, end: str, reason: str) -> str:
        """Report that a function really ends earlier: what follows `end` is alignment padding, linker leftovers
        or data, not code anyone wrote (or, in a PC project, another function the analysis merged with it), so no C
        can match the whole thing. ROM projects: the user approves it on the project page; then the function ends at
        `end` (an address) and the rest is set aside. PC projects: once agents of two models agree, the function is
        cut at `end` (an address, or an offset into it: +0x20) and the rest is a function of its own, set aside
        when it isn't code, kept only if the game still builds byte for byte. Say how you can tell."""
        return await run(ctx, "report_boundary", function=function, end=end, reason=reason)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def read_data(ctx: Context, what: str, length: int = 64) -> str:
        """Hex dump of the original's data: an address or symbol (ROM projects), a data symbol such as
        lbl_1_data_10 (forks); text, pointers and floats decoded."""
        return await run(ctx, "read_data", what=what, length=length)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def redraft(ctx: Context, function: str, context: str) -> str:
        """m2c's machine draft of a function made again with your declarations (a struct, a prototype, an extern
        you worked out) added to the shared header's; yours replace the header's where they declare the same
        names. Returns the new draft, recorded nowhere: try it with compile_and_diff, and edit_context what helped.
        PC games: Ghidra's draft made compilable again with them (a class declaring the function, a callee's
        prototype)."""
        return await run(ctx, "redraft", function=function, context=context)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def get_class(ctx: Context, what: str = "") -> str:
        """C++ classes found in the game (MIPS games' and PC programs' code; a PC program's run-time type
        information has the classes' own names): give a vtable address or a virtual function, constructor or destructor for its class's
        fields (offset, type, which functions use them) and virtual functions by slot; nothing lists them all. A
        layout to write the class from: the judge still decides."""
        return await run(ctx, "get_class", what=what)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def guess_struct(ctx: Context, function: str, argument: str = "arg0") -> str:
        """The likely struct behind a function's pointer (MIPS games, PC programs), as C with offsets: from the loads
        and stores of every function the same pointer reaches, with how many agree on each field, where else the
        type flows, and the type matched code already uses for it. argument: arg0-arg3 (PC: on the stack), `this`
        (PC: ecx), return, or a global; add +0x14 to follow the pointer at 0x14. The game's own type when its debug
        information has one. Put what you confirm into context.h with edit_context (a project's header with
        edit_file)."""
        return await run(ctx, "guess_struct", function=function, argument=argument)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def compiler_map(ctx: Context, survey: bool = False, check_ranges: bool = False) -> str:
        """Which compiler built each part of the game (ROM projects, MIPS; projects whose source files each have a
        compiler, PC and DS: over their files, with what the program itself says built it): where the code looks
        built by another compiler than the one it builds with today, from matches, habits that rule compilers out,
        and the judge. survey=true also starts the judge's survey in the background: agents' C for unmatched
        functions built with each compiler the project uses; what it confirms becomes a suggestion. A suggested range
        is tested on its neighbours before it applies (range checks, listed here); check_ranges=true looks at the
        applied ranges (projects: the open checks) again the same way."""
        return await run(ctx, "compiler_map", survey=survey, check_ranges=check_ranges)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def list_functions(ctx: Context, status: str = "open", limit: int = 40) -> str:
        """List functions by status: open, matched or all."""
        return await run(ctx, "list_functions", status=status, limit=limit)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def add_note(ctx: Context, text: str, function: str) -> str:
        """Leave a note on a function for the next agent on it. Say which function it's for: something every agent
        on this game should know goes through share_knowledge."""
        return await run(ctx, "add_note", text=text, function=function)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def search_flags(ctx: Context, function: str, compiler: str = "", source: str = "") -> str:
        """The function's C (its best attempt, or source) built with each of its compiler's flag variants
        (optimisation levels, small data, code-shape options), judged and ranked. For C that looks right but won't
        match: the flags may be what's wrong. compiler: another one to try instead of the project's. PC games: the
        function's source file's flags (/Oy- keeps the frame pointer, /O1, /Ob1); one that matches stays the file's."""
        return await run(ctx, "search_flags", function=function, compiler=compiler, source=source)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def claim_readable(ctx: Context) -> str:
        """Take the next matched function that isn't readable yet: rename, type and comment it so people understand
        it, while it still matches byte for byte."""
        return await run(ctx, "claim_readable")

    @mcp.tool(meta=ALWAYS_LOAD)
    async def make_readable(ctx: Context, function: str, source: str) -> str:
        """A readable rewrite of a matched function (names, types, a comment). Kept only if it still matches byte
        for byte; otherwise the matching C stays."""
        return await run(ctx, "make_readable", function=function, source=source)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def tidy_done(ctx: Context, unit: str, summary: str) -> str:
        """The source file claim_readable gave you to tidy is done (its types, names and declarations): say what you
        changed in a sentence or two."""
        return await run(ctx, "tidy_done", unit=unit, summary=summary)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def share_knowledge(ctx: Context, text: str = "", supports: int = 0) -> str:
        """Propose something every agent on this game should know (a convention, what a struct is, how the code
        is organised), or back an open proposal with supports=its number. It reaches every agent once helpers
        running two different models agree. Not for which compiler built what: that's the compiler map's job."""
        return await run(ctx, "share_knowledge", text=text, supports=supports)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def suggest(ctx: Context, text: str, compiler: str = "", flags: str = "", function: str = "",
                      ranges: str = "", source: str = "") -> str:
        """Suggest a change to the project's setup you can't make yourself, for the user to apply: above all a
        different compiler or flags, when C that is right can't match (the code's style says another compiler built
        it). Test it with try_compiler first: a suggestion whose function doesn't match with it is refused. Say what
        you saw. project_info lists open suggestions: suggesting the same compiler and flags is a vote for it; with
        the user's permission, a proven or agreed one is applied at once.

        Args:
            text: what should change and the evidence
            compiler: the compiler you think built the game (a toolchain id like ee-gcc-2.95.3)
            flags: its flags, if not the defaults
            function: a function whose best attempt should match with that compiler: it's checked right away
            ranges: when only parts of the game were built with it: "0x10BFD0-0x10CD40, 0x1106C0-0x1107C0"
            source: the C to check it with (default: the function's best attempt)
        """
        return await run(ctx, "suggest", text=text, compiler=compiler, flags=flags, function=function, ranges=ranges,
                         source=source)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def try_compiler(ctx: Context, function: str, compiler: str, flags: str = "", source: str = "") -> str:
        """Compile a function's C with another compiler and flags and see the diff, recording nothing: to find out
        which compiler built a part of the game before you suggest it. Cheap: try several.

        Args:
            function: the function to judge it against
            compiler: a toolchain id (project_info lists this project's); where each source file has its own
                compiler (DS), "all" tries every one, and the one that matches becomes the file's
            flags: its flags (default: that compiler's defaults for this game)
            source: the C (default: the function's best attempt)
        """
        return await run(ctx, "try_compiler", function=function, compiler=compiler, flags=flags, source=source)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def search_notes(ctx: Context, query: str) -> str:
        """Search notes left by agents."""
        return await run(ctx, "search_notes", query=query)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def finish_near_matches(ctx: Context, limit: int = 50, apply: bool = False) -> str:
        """Audit near matches; apply=true tries bounded source fixes and keeps only strict matches.
        Preserves matched neighbors and skips claims; targets, branches and local tables are never rewritten.
        """
        return await run(ctx, "finish_near_matches", limit=limit, apply=apply)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def solve_tiny_functions(ctx: Context, limit: int = 100, apply: bool = False) -> str:
        """Audit tiny return/trap/tail-call templates; apply=true keeps only strict matches, preserving neighbors.
        Uses existing simple signatures, skips claimed/matched functions, and examines at most 100 per pass.
        """
        return await run(ctx, "solve_tiny_functions", limit=limit, apply=apply)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def function_patterns(ctx: Context, function: str, limit: int = 5) -> str:
        """Retrieve matched donor patterns with source-convention support and exceptions; read-only advice."""
        return await run(ctx, "function_patterns", function=function, limit=limit)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def pattern_index_report(ctx: Context, sample: int = 10) -> str:
        """Report matched corpus coverage and bounded held-out retrieval diagnostics, never modifying matches."""
        return await run(ctx, "pattern_index_report", sample=sample)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def find_code_blocks(ctx: Context, function: str, limit: int = 5) -> str:
        """Find repeated straight-line fragments inside other functions, prioritizing recorded matched donors.
        Shows instruction offsets and operand-aware evidence, never changes source or match status. Constants,
        stack offsets and symbols are retained; register renaming is allowed. Search is bounded and reports coverage.
        """
        return await run(ctx, "find_code_blocks", function=function, limit=limit)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def find_similar(ctx: Context, function: str, limit: int = 5) -> str:
        """Functions whose instructions look like this one's (copy-pasted code, the same inlined helper,
        template instances), matched ones first, with the matching source of the best one. Start there."""
        return await run(ctx, "find_similar", function=function, limit=limit)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def guide(ctx: Context, topic: str = "") -> str:
        """Decompilation know-how. Call without a topic for the list (workflow, mips, gcc-kmc, ido, m2c,
        data, structs, n64, naming)."""
        return await run(ctx, "guide", topic=topic)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def read_file(ctx: Context, path: str = "", start: int = 1, count: int = 200, file_path: str = "",
                        offset: int = 0, limit: int = 0) -> str:
        """Forks only: read a file of the project (path relative to its root), with line numbers."""
        # file_path/offset/limit: the names of Claude Code's own Read tool, which agents passed here hundreds of
        # times a night, each a refused call
        if not (path := path or file_path):
            raise ValueError("read_file needs path: the file, relative to the project's root")
        return await run(ctx, "read_file", path=path, start=offset or start, count=limit or count)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def search_code(ctx: Context, pattern: str, glob: str = "") -> str:
        """Forks only: search the project's C/C++ sources and headers with a regular expression. `glob`
        narrows the files (e.g. "include/**/*.h")."""
        return await run(ctx, "search_code", pattern=pattern, glob=glob)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def edit_file(ctx: Context, path: str, old: str, new: str, message: str = "") -> str:
        """Forks only: change a C/C++ source or header file. Replace `old` (exactly once) with `new`, or
        append `new` when old is "". The project is rebuilt and measured; the edit is undone if any matched
        function stops matching. Use it for structs, prototypes, globals and helpers. `message`: what the
        change is, for its commit."""
        return await run(ctx, "edit_file", path=path, old=old, new=new, message=message)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def file_status(ctx: Context, path: str) -> str:
        """Forks only: how far a source file (or a function's file) is from being linked into the game: which
        functions still differ, its data next to the original's (with what to fix), why the last link failed.
        A file is linked (the game built from its C) once every function and all its data match."""
        return await run(ctx, "file_status", path=path)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def standins_for(ctx: Context, what: str) -> str:
        """Games built with link-time code generation (the brief's "Calling convention" section says so): a starting
        harness for a unit, from the original's references. `what`: a unit, its source file or one of its functions.
        Returns stand-ins for what the unit's functions call or read elsewhere (with their conventions), a
        HarnessEntry that calls each function the way its call sites do, and the declarations to put in the unit's
        source. Write them into harness/<unit>.standins.cpp and harness/<unit>.drv.cpp with edit_file (old=""
        creates a file), then shape them: the stand-ins must read the registers the original reads and leave the
        ones its callers keep alive."""
        return await run(ctx, "standins_for", what=what)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def split_file(ctx: Context, path: str, at: str, new_name: str = "", data: str = "") -> str:
        """Forks (GameCube, Wii, DS): divide a source file in two where the game's original files divided (DS: or
        to give a function built by another compiler version a file, and compiler, of its own: split at it and at the
        one after it, then try_compiler): the
        function `at` (name or .text offset) and every one after it in the file go to a new file `new_name`
        (default: text_<offset> next to it), with their C. `data` moves the end of each data section too
        (".rodata:0x88 .data:0x28": where the new file's part starts; file_status shows the ranges). Kept only if
        the game still builds byte for byte and every match still matches. A file links only when its code and
        data are exactly one original file's."""
        return await run(ctx, "split_file", path=path, at=at, new_name=new_name, data=data)

    @mcp.tool(meta=ALWAYS_LOAD)
    async def join_functions(ctx: Context, function: str, with_function: str, reason: str) -> str:
        """Forks: `function` and `with_function` are one function the game's analysis cut in two (or more: the
        symbols between them too). A piece with no prologue that uses registers the code before it set, an epilogue
        alone, a switch whose cases are in the next symbol. Say what in the code shows it. Once agents of two models
        say so of the same two, they're joined, if the game still builds byte for byte; never a matched function."""
        return await run(ctx, "join_functions", function=function, with_function=with_function, reason=reason)

    @mcp.prompt(title="Decompile", description="Work loop: claim functions and match them byte for byte.")
    def work(count: str = "5") -> str:
        return "Call project_info first, then follow the decompdecomp work loop for up to " + count + " functions."

    return mcp
