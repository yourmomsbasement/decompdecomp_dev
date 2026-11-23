"""The permuter, in the background: the decomp community's way through the last few percent.

A function at 95% usually differs in register allocation or instruction order, which comes down to how the C
is written, not what it does: the order of statements and declarations, a temporary, a cast, `x += 1` for
`x++`, an if turned around. Agents doing it by hand spend turns (and tokens) on it, and stall. Here it runs on its
own, on spare CPU, on the near misses agents left (or autopilot's drafts reached): a worker holds a function for a
turn of a minute, so an agent that wants it gets it next, records every improvement as the function's best attempt
(agents start from it), and a match as a match. What matches is still judged byte for byte, like everything else.

Variants come from decomp-permuter (MIT, a pinned tool: Compilers & tools, or `decomp setup`): permuter_gen.py, run
in the tool's own environment, rewrites the function's C on its parse tree, BATCH variants at a time, with the
weights decomp-permuter tuned for the function's compiler. Our own judge scores each one (`_score`): a variant that
doesn't compile is skipped. Its rewrites don't all keep the meaning (it moves statements, retypes, deletes code), so
only what the judge says settles anything: a match is the same machine code as the game's, and an improvement is
only closer code for an agent to start from. Without the tool, or for C it can't read (a macro call of the source's
own), the six simple rewrites of permuter.py make the variants instead.

It runs on the background workers every project shares with the machine drafts (workers.py: one per spare core, up
to four), each at a lower CPU priority that the compilers it runs inherit: the app and the agents' compiles come first.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import wait
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from .. import consoles, permuter_gen, scoring, seeds, toolchains
from ..forks import csource, objdiff
from ..hostenv import NO_WINDOW
from ..network import NetworkError
from ..permuter import mutate
from ..project import compiler_for
from . import workers

log = logging.getLogger("decompdecomp")
NEAR = 0.06  # a near miss: at most this much of the function differs (best_score <= 6% of n_insns * 100)
SECONDS = 60  # per function per turn
MAX_TURNS = 4  # turns per function before it's left to agents (counted in the database: functions.permuted)
CANDIDATES = 20  # near misses looked up at a time, for the workers to share
TOOL = "decomp-permuter"
GENERATOR = Path(__file__).resolve().parent.parent / "permuter_gen.py"
BATCH = 64  # variants per run of the generator: about 0.15 s to write, a few seconds to judge
GENERATOR_SECONDS = 30  # a batch that takes longer is stuck: its randomizer retries until a rewrite applies
READ_BYTES = 1 << 16
MAX_SEED = 2**31  # seeds of the generator's runs: the same function and turn write the same variants
REWRITE_MISSES = 200  # simple rewrites in a row that change nothing: none applies to the C
MAX_INSNS = 400  # bigger near misses are left to agents: one variant takes too long to judge
IDLE_SECONDS = 180  # with no near misses, how long until the permuter looks again
IDLE_LOOKS = 20  # looks in a row with nothing to do (an hour) and it's done until agents start again
_NAME = re.compile(r"[A-Za-z_]\w*")
_CXX = re.compile(r"::|\btemplate\s*<|\b(?:class|operator|static_cast|reinterpret_cast|this)\b")
_COMMENT_OR_STRING = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)
HOLD_SPARE_SECONDS = 120
PREVIEW_FAILED = -1  # a variant whose scratch build failed (permute._preview)
PREVIEW_TRIES = 3  # a turn's first previews that all fail to build: previews off for that turn (the build, not the C)  # a turn's hold on a function lasts this much longer than the turn, in case it runs over
_EOF = object()
# decomp-permuter's weights per compiler (its default_weights.toml), by the start of a toolchain's id
FAMILIES = (("ido", "ido"), ("gcc", "gcc"), ("ee-gcc", "gcc"), ("psp-gcc", "gcc"), ("ps3", "gcc"), ("agbcc", "gcc"),
            ("mwc", "mwcc"))


def weight_family(cfg: dict, vram: int | None, fork: bool) -> str:
    """Which of decomp-permuter's weight sets suits the compiler that builds the code at `vram`."""
    if fork:  # forks name their console, not a toolchain: the console says its compilers' family
        return consoles.get(cfg.get("platform")).compiler_family or "base"
    toolchain = (compiler_for(cfg, vram).get("toolchain") or "") if cfg.get("compiler") else ""
    return next((family for prefix, family in FAMILIES if toolchain.startswith(prefix)), "base")


def available() -> bool:
    return toolchains.installed(TOOL) is not None


def permuter_header(header: str, source: str) -> str:
    """The part of `header` the function's C uses (seeds.HeaderIndex), which is all decomp-permuter has to read.
    One construct pycparser can't read anywhere in the header stopped it on every function: an x86 game's headers
    are C++ with MSVC's words, thousands of lines of them, where one function needs a few dozen declarations.
    MSVC's words go first (permuter_gen.msvc_as_c): `struct __declspec(align(16)) CVec4` was a struct named
    `__declspec` to the reader, so the vector type every near miss used was left out."""
    index = seeds.HeaderIndex(permuter_gen.msvc_as_c(header))
    return "\n".join(d.text for d in index.used(_NAME.findall(source)))


def is_cxx(source: str) -> bool:
    """A function written in C++: a method or a scoped name, a template, a class, an operator, a C++ cast, `this`;
    in its code, not in a comment or a string (`/* this adds one */` is C's)."""
    return bool(_CXX.search(_COMMENT_OR_STRING.sub(" ", source)))


def generator_argv() -> list[str]:
    tool = toolchains.path(TOOL)
    return [str(toolchains.venv_bin(tool / "venv", "python")), "-I", str(GENERATOR), str(tool)]


def _own(source: str, name: str) -> str:
    """The function's own definition from the C a turn starts on. decomp-permuter hands back whatever was handed to
    it, and the attempt it is scored by must be one function definition plus the declarations it needs: a unit's
    neighbours in the source come back in every variant and are refused, which burnt the turn."""
    return csource.get(source, name) or source


class Generator:
    """The generator (permuter_gen.py) for one turn, in decomp-permuter's environment. It stays up for the turn, so
    it parses the header once per C it starts from: that takes a second for a header of 300 KB (debug symbols give a
    game thousands of types), where writing a batch of 64 variants takes 0.1 s."""

    def __init__(self, argv: list[str] | None = None):
        self.proc = subprocess.Popen(argv or generator_argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=NO_WINDOW | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        # Windows select() accepts sockets only. A reader thread also gives pipe reads a deadline there.
        self._lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True, name="permuter-output").start()

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(_EOF)

    def batch(self, job: dict) -> list[str] | None:
        """Variants of a function's C (the job's fields: permuter_gen.py), or None when the generator can't read
        the C, has stopped or doesn't answer: then the simple rewrites take over."""
        try:
            self.proc.stdin.write(json.dumps(job).encode() + b"\n")
            self.proc.stdin.flush()
        except OSError:
            return None
        deadline = time.monotonic() + GENERATOR_SECONDS
        variants: list[str] | None = []
        while True:
            line = self._line(deadline)
            if line is None or line is _EOF:
                if line is _EOF:
                    try:
                        self.proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                log.info("permuter: the generator stopped or hung on %s", job["function"])
                self.close()
                return None
            try:
                out = json.loads(line)
            except ValueError:
                continue
            if not isinstance(out, dict):
                continue
            if "done" in out:
                return variants
            if "error" in out:  # read on to its "done": the next job's answers start after it
                log.info("permuter: decomp-permuter can't read %s: %s", job["function"], str(out["error"])[:300])
                variants = None
            elif variants is not None and isinstance(out.get("variant"), str):
                variants.append(out["variant"])
    def _line(self, deadline: float):
        """The generator's next line, or None when it has stopped or hasn't answered by `deadline`."""
        try:
            return self._lines.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            return None

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


@dataclass
class State:
    state: str = "running"
    tried: int = 0  # functions permuted
    variants: int = 0
    improved: int = 0
    matched: int = 0
    current: str = ""  # the functions the workers hold now
    started_at: float = field(default_factory=time.time)
    stop: threading.Event = field(default_factory=threading.Event)
    _held: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, **counts: int) -> None:
        with self._lock:  # every worker counts here
            for name, n in counts.items():
                setattr(self, name, getattr(self, name) + n)

    def holding(self, name: str, held: bool) -> None:
        with self._lock:
            if held:
                self._held.append(name)
            else:
                self._held.remove(name)
            self.current = ", ".join(self._held)


class Permuter:
    def __init__(self, net):
        self.net = net
        self.jobs: dict[str, State] = {}
        self._previews: dict[str, list[int]] = {}  # a turn's scratch folder -> [scratch builds that built, that failed]

    def status(self, slug: str) -> dict | None:
        st = self.jobs.get(slug)
        return None if st is None else {k: v for k, v in st.__dict__.items() if k != "stop" and not k.startswith("_")}

    def start(self, slug: str) -> State | None:
        """Run on the project's near misses until there are none (looking again every few minutes while the
        app runs: agents make new ones)."""
        if (st := self.jobs.get(slug)) is not None and st.state == "running":
            return st
        if slug in self.net.links:  # a room member: the host's copy permutes, and its matches come back here
            return None
        st = self.jobs[slug] = State()
        threading.Thread(target=self._loop, args=(slug, st), daemon=True, name=f"permuter-{slug}").start()
        return st

    def stop(self, slug: str) -> None:
        if (st := self.jobs.get(slug)) is not None:
            st.stop.set()

    def candidates(self, proj, limit: int = CANDIDATES) -> list:
        return self.net.store.all(
            "SELECT * FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL "
            "AND best_score > 0 AND best_score <= n_insns * 100 * ? AND n_insns <= ? AND permuted < ? "
            "AND id NOT IN (SELECT function_id FROM claims) ORDER BY CAST(best_score AS REAL) / (n_insns * 100) LIMIT ?",
            proj["id"], NEAR, MAX_INSNS, MAX_TURNS, limit)

    def _loop(self, slug: str, st: State) -> None:
        agent, _ = self.net.local_agent("permuter")
        idle = 0
        try:
            while not st.stop.is_set():
                try:
                    proj = self.net.project_row(slug)
                except NetworkError:
                    return  # the project is gone
                todo = self.candidates(proj)
                if not todo:
                    idle += 1
                    if idle > IDLE_LOOKS:
                        return
                    st.stop.wait(IDLE_SECONDS)
                    continue
                idle = 0
                wait([workers.pool().submit(self._turn, agent, proj, fn, st) for fn in todo])
        finally:
            st.state = "stopped" if st.stop.is_set() else "done"

    def _turn(self, agent, proj, fn, st: State) -> None:
        try:
            self._permute(agent, proj, fn, st)
        except NetworkError as e:  # the project refused its source (a helper the file hasn't): an answer, not a crash
            log.info("permuter: %s: %s (left to agents)", fn["name"], str(e).splitlines()[0])
            with self.net.store.tx() as db:  # the same best attempt is refused every time: no more turns on it
                db.execute("UPDATE functions SET permuted=MAX(permuted, ?) WHERE id=?", (MAX_TURNS, fn["id"]))
        except Exception as e:  # one odd function must not stop the rest
            log.info("permuter: %s: %s", fn["name"], e, exc_info=True)

    def _hold(self, agent, fn, *, count_turn: bool = True) -> bool:
        now = time.time()
        with self.net.store.tx() as db:
            self.net._expire_claims(db)
            if db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                return False
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + SECONDS + HOLD_SPARE_SECONDS))
            if count_turn:
                db.execute("UPDATE functions SET permuted = permuted + 1 WHERE id=?", (fn["id"],))
        return True

    def _renew(self, agent, fn) -> bool:
        """The turn's hold counted from now; False when it's no longer this turn's."""
        now = time.time()
        with self.net.store.tx() as db:
            return db.execute("UPDATE claims SET expires_at=? WHERE function_id=? AND agent_id=? AND expires_at>?",
                              (now + SECONDS + HOLD_SPARE_SECONDS, fn["id"], agent.id, now)).rowcount > 0

    def _random_seed(self, fn):
        return f"{fn['id']}-{fn['permuted']}"

    def _turn_deadline(self):
        return time.monotonic() + SECONDS

    def _let_go(self, agent, fn) -> None:
        with self.net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))

    def _permute(self, agent, proj, fn, st: State) -> None:
        if st.stop.is_set() or not self._hold(agent, fn):
            return
        st.add(tried=1)
        st.holding(fn["name"], True)
        fork = self.net.fork_slug(proj["slug"])
        rng = random.Random(self._random_seed(fn))
        gen = None
        held = ExitStack()
        try:
            if fork:  # its unit is held for the turn and built once at its end, not after every variant (trial_run)
                held.enter_context(self.net.forks.trial_run(proj, fn["unit"]))
                # (waiting for another turn on the unit can take most of the hold: it's counted from here, and a hold
                # that ran out meanwhile, the function an agent's now, ends the turn)
                if not self._renew(agent, fn):
                    return
            if available():
                # (a C function's as m2c reads it: what's only for C++ made pycparser refuse a PC game's whole
                # header; a C++ function's with its C++ branch, where its classes are)
                header = permuter_header(seeds.c_only(self.net.forks.permuter_header(fork, fn["name"]) if fork else
                                                      self.net._context(proj["id"], fn["unit"])["header"],
                                                      cxx=is_cxx(fn["best_source"])), fn["best_source"])
                family = weight_family(self.net.cfg(proj), fn["vram"], bool(fork))
                name = self.net.forks.permuter_name(fork, fn["name"], fn["best_source"]) if fork else fn["name"]
                try:
                    gen = Generator()
                except OSError as e:  # its environment is broken: the simple rewrites still work
                    log.info("permuter: decomp-permuter didn't start: %s", e)
            start_src = best_src = cur_src = _own(fn["best_source"], fn["name"])
            best = cur = fn["best_score"]
            deadline = self._turn_deadline()
            # a scratch copy of the unit's build for this turn's previews (fork: Console.build_unit_with)
            scratch = tempfile.TemporaryDirectory(prefix="decompdecomp-permute-") if fork else None
            batches = misses = 0
            refused, taken = None, 0
            while time.monotonic() < deadline and best > 0 and not st.stop.is_set():
                if gen is not None:
                    # a batch of decomp-permuter's variants, of the C the turn started from and of the best so far in
                    # turn: from the best alone, rewrites piled on rewrites and got stuck on one of nine near misses of
                    # the demo ROM (at 45); taking turns, every one matched within 12 s
                    base = best_src if batches % 2 else start_src
                    batches += 1
                    batch = gen.batch({"header": header, "source": base, "function": name, "family": family,
                                       "seed": rng.randrange(MAX_SEED), "count": BATCH})
                    if batch is None:  # C it can't read: the simple rewrites can still try
                        gen.close()
                        gen = None
                        continue
                    if not batch:  # nothing new left to write from this C
                        break
                else:  # one simple rewrite, mostly of the last C kept
                    batch = [mutate(cur_src if rng.random() < 0.85 else best_src, rng)]
                    # none of them applies to this C (lerp's `a + (b - a) * t`): a minute of trying burnt a core
                    misses = 0 if batch[0] else misses + 1
                    if misses >= REWRITE_MISSES:
                        break
                for cand in batch:
                    if cand is None or time.monotonic() >= deadline or st.stop.is_set():
                        break
                    st.add(variants=1)
                    try:
                        score = self._score(agent, proj, fn, cand, best, fork, Path(scratch.name) if scratch else None)
                    except NetworkError as e:
                        # A variant the project won't take: decomp-permuter's inlining writes a helper function
                        # beside the function, which compile_and_diff refuses in a fork. It's skipped like one that
                        # doesn't compile; the refusal used to end the function's permuting for good (_turn) within
                        # seconds of its first turn: 2 of 3 of an Xbox game's near misses
                        refused = e
                        continue
                    taken += 1
                    if score is None:
                        continue
                    if score < best:
                        best, best_src = score, cand
                        st.add(improved=1)
                        if score == 0:
                            st.add(matched=1)
                            log.info("permuter: %s matched", fn["name"])
                            break
                    if score <= cur or rng.random() < 0.02:  # mostly downhill, now and then sideways off a plateau
                        cur, cur_src = score, cand
            if refused is not None and not taken:
                raise refused
        finally:
            try:
                if gen is not None:
                    gen.close()
                if "scratch" in locals() and scratch is not None:
                    self._previews.pop(scratch.name, None)
                    scratch.cleanup()
                held.close()
            finally:
                st.holding(fn["name"], False)
                self._let_go(agent, fn)

    def _score(self, agent, proj, fn, source: str, best: int, fork: str | None, scratch: Path | None = None) -> int | None:
        """A variant's score; recorded (as the function's best attempt, or its match) only when it beats `best`. In a
        fork, a preview from a scratch build of the unit first (`_preview`): only a variant that beats `best` there
        goes through the project's judge, which builds in the project's tree, protects the unit's matches and records
        it. Judged in the tree, each variant cost three builds of its unit beside the helpers' (its baseline, the
        variant, the file put back): on a PS4 game a 60-second turn judged about 5 variants."""
        with self.net.progress.judging(proj["slug"]):
            if fork:
                quick = self._preview(proj, fn, source, scratch) if scratch is not None else None
                if quick == PREVIEW_FAILED:
                    return None  # it doesn't build: nothing to judge
                if quick is not None and quick >= best:
                    return quick  # no better: the project's tree is never touched for it
                try:
                    res = self.net.forks.attempt(agent, fork, fn["name"], source, keep=False, quiet=best)
                except NetworkError as e:  # a variant the project won't take (a helper beside the function): the next one
                    # (and why: 54 refusals on a PS4 game said only "refused", so nobody could tell what stopped them)
                    log.debug("permuter: %s: variant refused: %s", fn["name"], str(e).splitlines()[0][:200])
                    return None
                return res.get("score")
            ctx = self.net._context(proj["id"], fn["unit"])
            result = self.net._compile(proj, ctx["header"], source, fn)
            if not result.ok:
                return None
            cand, verdict, _ = self.net._evaluate(fn, proj, result.obj)
            if cand is None:
                return None
            d = scoring.diff(self.net._target_obj(fn, proj), cand)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            if d.score < best:
                self.net._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match, via="permuter")
            return 0 if d.bytes_match else max(1, d.score)

    def _preview(self, proj, fn, source: str, scratch: Path) -> int | None:
        """The variant's score from a build of its unit outside the project's tree, its definition in place of the
        function's (Console.build_unit_with, the unit's own command); PREVIEW_FAILED: it doesn't build; None: no
        preview here (a console that can't, a variant with more than its definition: the project's judge places those)."""
        from .forkengine import scaled

        engine = self.net.forks
        try:
            _, fp, cfg = engine.get(proj["slug"])
            unit = fp.unit(fn["unit"])
            if not unit.source or unit.target is None or not unit.target.exists():
                return None
            names = engine._names(engine._meta(fn), fn)
            _, span = engine._find(source, names)
            if span is None or source[:span[0]].strip() or source[span[1]:].strip():
                return None
            text = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
            _, there = engine._find(text, names)
            if there is None:
                return None
            console = consoles.get(cfg.get("platform"))
            if type(console).build_unit_with is consoles.Console.build_unit_with:
                return None  # this console builds only in its tree
            tally = self._previews.setdefault(str(scratch), [0, 0])  # (built, failed) this turn
            if tally[1] >= PREVIEW_TRIES and not tally[0]:
                return None  # its scratch builds fail whatever the variant: the project's judge decides each
            # (placed as the project's judge places a definition: ForkEngine._attempt)
            obj = console.build_unit_with(fp, unit, None, scratch, text[:there[0]] + source[span[0]:span[1]].rstrip("\n")
                                          + "\n" + text[there[1]:])
            tally[0 if obj is not None else 1] += 1
            if obj is None:
                return PREVIEW_FAILED
            d = objdiff.diff(unit.target, obj, engine._meta(fn).get("symbol") or fn["name"], cwd=fp.root,
                             options=fp.diff_options(unit))
        except Exception:  # (a preview is only a shortcut: the project's judge is the fallback)
            log.debug("permuter: %s: no preview", fn["name"], exc_info=True)
            return None
        if d.match_percent is None:
            return PREVIEW_FAILED
        return max(1, len(d.literals)) if d.literals else scaled(d.match_percent, fn["n_insns"])