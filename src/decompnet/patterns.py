"""Matched-function retrieval and evidence summaries, never a substitute for the byte-match judge.

Mnemonic sequences retrieve candidates without guessing source semantics. Source traits are observed only after
retrieval, so held-out source-trait agreement does not feed the ranking it measures.
"""

import math
import random
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

MAX_OPS = 12000
MAX_SOURCE = 65536
TRAVERSAL_BUNDLE = 'acquire load + acq_rel signal fence + btver2 (co-occurrence; necessity untested)'
WEAK_NOINLINE = 'weak + noinline attribute (symbol binding changes)'


def _calls(clean, name):
    """Read balanced call arguments so a later unrelated acquire token cannot qualify a relaxed load."""
    for match in re.finditer(r'\b'+re.escape(name)+r'\s*\(', clean):
        start, depth, args = match.end(), 1, []
        for i in range(start, len(clean)):
            char = clean[i]
            depth += (char == '(') - (char == ')')
            if char == ',' and depth == 1:
                args.append(clean[start:i].strip())
                start = i+1
            elif depth == 0:
                args.append(clean[start:i].strip())
                yield args
                break
            elif char in ';{}':
                break



@lru_cache(maxsize=512)
def source_traits(source):
    """Recognize explicit spellings in saved C; do not infer causality or the original author's intent."""
    # Comments and ordinary strings must not count as source conventions. Preserve target attribute strings only.
    clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
    attrs = re.findall(r'\btarget\s*\(\s*"([^"\n]+)"\s*\)', clean)
    clean = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', clean)
    traits = {'target attribute '+a for a in attrs if re.fullmatch(r'[\w=,+.-]{1,80}', a)}
    for label, pattern in {
        'signal fence': r'\b__atomic_signal_fence\s*\(',
        'volatile spelling': r'\bvolatile\b',
        'explicit unsigned type': r'\b(?:unsigned|uint(?:8|16|32|64)_t)\b',
        'switch statement': r'\bswitch\s*\(',
        'loop statement': r'\b(?:for|while)\s*\(',
        'vector intrinsic spelling': r'\b_mm(?:256|512)?_\w+\s*\(',
        'memcpy call': r'\b(?:__builtin_)?memcpy\s*\(',
    }.items():
        if re.search(pattern, clean):
            traits.add(label)
    if any(len(args) == 2 and args[1] == '__ATOMIC_ACQUIRE' for args in _calls(clean, '__atomic_load_n')):
        traits.add('acquire atomic load')
    if any(args == ['__ATOMIC_ACQ_REL'] for args in _calls(clean, '__atomic_signal_fence')):
        traits.add('acq_rel signal fence')
    for attribute in re.findall(r'\b__attribute__\s*\(\(([^()]*)\)\)', clean):
        names = {part.strip() for part in attribute.split(',')}
        if {'weak', 'noinline'} <= names:
            traits.add(WEAK_NOINLINE)
    if {'acquire atomic load', 'acq_rel signal fence', 'target attribute arch=btver2'} <= traits:
        traits.add(TRAVERSAL_BUNDLE)
    return frozenset(traits)


def features(ops):
    ops = tuple(op.lower() for op in ops[:MAX_OPS])
    # Ordered triples distinguish families with similar instruction counts; counts are log-scaled below.
    counts = Counter(('op', op) for op in ops)
    counts.update(('pair', *ops[i:i+2]) for i in range(len(ops)-1))
    counts.update(('triple', *ops[i:i+3]) for i in range(len(ops)-2))
    return counts


@dataclass(frozen=True)
class Entry:
    id: int
    name: str
    unit: str
    ops: tuple
    source: str = ''


class Index:
    """One immutable corpus snapshot. Rebuilt when available targets or recorded matched source changes."""
    def __init__(self, entries):
        self.entries = tuple(entries)
        raw = [features(e.ops) for e in entries]
        frequency = Counter(k for row in raw for k in row)
        self.idf = {k: 1 + math.log((1+len(raw))/(1+v)) for k, v in frequency.items()}
        self.vectors = tuple(self.vector(row) for row in raw)

    def vector(self, counts):
        vector = {k: (1+math.log(v))*self.idf.get(k, 1+math.log(1+len(self.entries))) for k, v in counts.items()}
        norm = math.sqrt(sum(v*v for v in vector.values())) or 1
        return {k: v/norm for k, v in vector.items()}

    def rank(self, ops, exclude=None, limit=5, exclude_clones=False, source=''):
        """Similarity is a retrieval score, not a probability of a match. Never rank using saved C traits."""
        query = self.vector(features(ops))
        ranked = []
        for entry, vector in zip(self.entries, self.vectors):
            if entry.id == exclude:
                continue
            if exclude_clones and (entry.ops == tuple(ops) or (source and re.sub(r'\s+', '', source) == re.sub(r'\s+', '', entry.source))):
                continue
            score = sum(value * vector.get(key, 0) for key, value in query.items())
            score *= (min(len(ops), len(entry.ops))/max(len(ops), len(entry.ops), 1)) ** .25
            if score >= .45:
                ranked.append((score, entry))
        return sorted(ranked, key=lambda item: (-item[0], item[1].id))[:limit]


def evidence(entries):
    """Report support and exceptions in the retrieved sample, not project-wide compiler rules."""
    readable = [entry for entry in entries if entry.source and len(entry.source) <= MAX_SOURCE]
    counts = Counter(trait for entry in readable for trait in source_traits(entry.source))
    return [(trait, count, len(readable), [e.name for e in readable if trait not in source_traits(e.source)])
            for trait, count in sorted(counts.items(), key=lambda item: (item[0] not in {TRAVERSAL_BUNDLE, WEAK_NOINLINE}, -item[1], item[0]))]


def evaluate(entries, sample=40):
    """Held-out retrieval proxy: source-trait overlap, excluding identical opcode/source clones.

    Refit each training index without the held-out entry to prevent even IDF leakage. This measures retrieval
    usefulness only, never decompilation success, time saved, or semantic family accuracy.
    """
    eligible = [e for e in entries if e.source and len(e.source) <= MAX_SOURCE and source_traits(e.source)]
    selected = eligible[:sample] if len(eligible) <= sample else [eligible[i*len(eligible)//sample] for i in range(sample)]
    retrieved = agreed = baseline_agreed = 0
    for query in selected:
        index = Index([e for e in entries if e.id != query.id])
        hits = index.rank(query.ops, exclude=query.id, limit=3, exclude_clones=True, source=query.source)
        retrieved += bool(hits)
        agreed += any(source_traits(query.source) & source_traits(e.source) for _, e in hits)
        pool = [e for e in entries if e.id != query.id and e.ops != query.ops
                and re.sub(r'\s+', '', e.source) != re.sub(r'\s+', '', query.source)]
        control = random.Random(query.id).sample(pool, min(3, len(pool)))
        baseline_agreed += any(source_traits(query.source) & source_traits(e.source) for e in control)
    return {'tested': len(selected), 'retrieved': retrieved, 'trait_agreement': agreed, 'random_trait_agreement': baseline_agreed}
