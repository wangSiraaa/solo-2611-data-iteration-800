"""Regional-edition solver: one consistent set of usages across the whole book.

A regional edition derives from one *actually published* package.  For the
selected chunks, regional editors supply a limited set of whole-sentence
replacement drafts.  A draft carries the complete text, exactly one usage
number per marker of the chunk, and a non-negative cost.  A draft that
assigns the same concept two different values inside one chunk is unusable.
Regions may pre-lock usages for some concepts.

The solver picks one draft per chunk such that every concept takes a single
value across the whole book, minimizing total cost; ties are broken by the
lexicographically smallest sequence of draft ids in package chunk order.
When no feasible edition exists it reports an irreducible set of mutually
incompatible constraints and produces nothing — never half a book.

This module is pure: no I/O, no database.  Limits: at most 12 concepts and
at most 3 usages per concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .errors import (
    InvalidRegionalLock,
    RegionalProblemTooLarge,
)
from .terminology import MarkerResolution

MAX_CONCEPTS = 12
MAX_USAGES = 3


@dataclass(frozen=True)
class ChunkView:
    """One chunk of the published base package, in package order."""

    translation_id: str
    source_ids: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class RegionalQuote:
    """One whole-sentence replacement draft offered by a regional editor."""

    document_id: str
    region: str
    translation_id: str
    draft_id: str
    text: str
    assignments: tuple[tuple[str, int], ...]  # (marker_id, usage), sorted
    cost: int

    def assignments_dict(self) -> dict[str, int]:
        return dict(self.assignments)


@dataclass(frozen=True)
class DraftOption:
    """A quote that passed per-chunk usability checks."""

    draft_id: str
    text: str
    cost: int
    assignments: tuple[tuple[str, int], ...]  # (marker_id, usage), sorted
    concept_values: tuple[tuple[str, int], ...]  # (concept_id, usage), sorted


@dataclass(frozen=True)
class RegionalChoice:
    """What one chunk ends up with in a solved edition."""

    translation_id: str
    draft_id: Optional[str]  # None -> original package text kept
    text: str
    assignments: tuple[tuple[str, int], ...]
    concept_values: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class RegionalDiagnosis:
    """Why no feasible regional edition exists (nothing is produced)."""

    reason: str  # "no_usable_draft" | "inconsistent_constraints"
    detail: str
    conflicts: tuple[dict[str, object], ...]
    rejected_drafts: tuple[tuple[str, str, str], ...]  # (translation_id, draft_id, why)

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "conflicts": [dict(c) for c in self.conflicts],
            "rejected_drafts": [list(x) for x in self.rejected_drafts],
        }


@dataclass(frozen=True)
class RegionalPlan:
    ok: bool
    region: str
    terminology_version: int
    choices: tuple[RegionalChoice, ...]
    total_cost: Optional[int]
    concept_values: tuple[tuple[str, int], ...]
    diagnosis: Optional[RegionalDiagnosis]


# ----------------------------------------------------------------------
# per-chunk draft usability
# ----------------------------------------------------------------------
def _chunk_options(
    chunk: ChunkView,
    markers: Sequence[MarkerResolution],
    quotes: Sequence[RegionalQuote],
    max_usages: int,
) -> tuple[list[DraftOption], list[tuple[str, str, str]]]:
    """Usable drafts for one chunk plus per-draft rejection reasons."""

    chunk_markers = [m for m in markers if m.segment_id in chunk.source_ids]
    if not chunk_markers:
        return [], []
    marker_ids = {m.marker_id for m in chunk_markers}
    concept_of = {m.marker_id: m.concept_id for m in chunk_markers}

    options: list[DraftOption] = []
    rejected: list[tuple[str, str, str]] = []
    for quote in sorted(quotes, key=lambda q: q.draft_id):
        assigned = quote.assignments_dict()
        if set(assigned) != marker_ids:
            rejected.append(
                (
                    quote.translation_id,
                    quote.draft_id,
                    "draft must assign each chunk marker exactly once "
                    f"(chunk markers {sorted(marker_ids)!r}, "
                    f"draft assigns {sorted(assigned)!r})",
                )
            )
            continue
        bad_value = next(
            (v for v in assigned.values() if v < 1 or v > max_usages), None
        )
        if bad_value is not None:
            rejected.append(
                (
                    quote.translation_id,
                    quote.draft_id,
                    f"usage number {bad_value} outside 1..{max_usages}",
                )
            )
            continue
        concept_values: dict[str, int] = {}
        conflict: Optional[str] = None
        for marker_id, value in assigned.items():
            concept = concept_of[marker_id]
            previous = concept_values.get(concept)
            if previous is not None and previous != value:
                conflict = (
                    f"concept {concept!r} is assigned both {previous} and "
                    f"{value} inside one chunk"
                )
                break
            concept_values[concept] = value
        if conflict is not None:
            rejected.append((quote.translation_id, quote.draft_id, conflict))
            continue
        options.append(
            DraftOption(
                draft_id=quote.draft_id,
                text=quote.text,
                cost=quote.cost,
                assignments=quote.assignments,
                concept_values=tuple(sorted(concept_values.items())),
            )
        )
    return options, rejected


def _lock_filter(
    options: Sequence[DraftOption], locks: Mapping[str, int]
) -> list[DraftOption]:
    return [
        option
        for option in options
        if all(locks.get(concept, value) == value for concept, value in option.concept_values)
    ]


# ----------------------------------------------------------------------
# feasibility / optimization
# ----------------------------------------------------------------------
def _future_concepts(
    chunk_options: Sequence[Sequence[DraftOption]],
) -> list[set[str]]:
    """Concepts still needed after each chunk (for state projection)."""

    n = len(chunk_options)
    future: list[set[str]] = [set() for _ in range(n)]
    running: set[str] = set()
    for i in range(n - 1, -1, -1):
        future[i] = running
        running = running | {
            concept for option in chunk_options[i] for concept, _ in option.concept_values
        }
    return future


def _sweep(
    chunk_options: Sequence[Sequence[Optional[DraftOption]]],
    *,
    optimize: bool,
) -> Optional[tuple[int, tuple[str, ...], tuple[Optional[DraftOption], ...]]]:
    """DP over chunks in package order.

    State = assignment restricted to concepts that future chunks still
    mention.  Dominance: for one state, a prefix with smaller (cost, seq)
    always wins.  Returns (cost, draft-id sequence, chosen options) or None.
    """

    future = _future_concepts([[o for o in opts if o] for opts in chunk_options])
    # state -> (cost, draft-id sequence, chosen options so far)
    states: dict[
        frozenset[tuple[str, int]],
        tuple[int, tuple[str, ...], tuple[Optional[DraftOption], ...]],
    ] = {frozenset(): (0, (), ())}
    for index, options in enumerate(chunk_options):
        new_states: dict[
            frozenset[tuple[str, int]],
            tuple[int, tuple[str, ...], tuple[Optional[DraftOption], ...]],
        ] = {}
        for state, (cost, seq, chosen) in states.items():
            for option in options:
                if option is None:
                    candidate = (cost, seq, chosen + (None,))
                    key = state
                else:
                    clash = any(
                        dict(state).get(concept, value) != value
                        for concept, value in option.concept_values
                    )
                    if clash:
                        continue
                    candidate = (
                        cost + option.cost,
                        seq + (option.draft_id,),
                        chosen + (option,),
                    )
                    key = state | frozenset(option.concept_values)
                if not optimize:
                    candidate = (0, candidate[1], candidate[2])
                previous = new_states.get(key)
                if previous is None or candidate[:2] < previous[:2]:
                    new_states[key] = candidate
        if not new_states:
            return None
        keep = future[index]
        projected: dict[
            frozenset[tuple[str, int]],
            tuple[int, tuple[str, ...], tuple[Optional[DraftOption], ...]],
        ] = {}
        for state, value in new_states.items():
            shrunk = frozenset((c, v) for c, v in state if c in keep)
            previous = projected.get(shrunk)
            if previous is None or value[:2] < previous[:2]:
                projected[shrunk] = value
        states = projected
    best = min(states.values(), key=lambda v: v[:2])
    return best


# ----------------------------------------------------------------------
# conflict core
# ----------------------------------------------------------------------
def _feasible(
    active_chunks: Sequence[int],
    options_by_chunk: Mapping[int, Sequence[DraftOption]],
    locks: Mapping[str, int],
) -> bool:
    filtered: list[list[Optional[DraftOption]]] = []
    for index in active_chunks:
        usable = _lock_filter(options_by_chunk[index], locks)
        if not usable:
            return False
        filtered.append(usable)
    if not filtered:
        return True
    return _sweep(filtered, optimize=False) is not None


def _conflict_core(
    chunk_options: Mapping[int, Sequence[DraftOption]],
    locks: Mapping[str, int],
) -> tuple[dict[str, int], tuple[int, ...]]:
    """An irreducible set of mutually incompatible constraints.

    Every lock and every chunk left in the result is necessary: dropping any
    one of them makes the remainder feasible.
    """

    core_locks = dict(locks)
    core_chunks = sorted(chunk_options)
    for concept in sorted(list(core_locks)):
        trial = {c: v for c, v in core_locks.items() if c != concept}
        if not _feasible(core_chunks, chunk_options, trial):
            core_locks = trial
    for index in list(core_chunks):
        trial_chunks = [i for i in core_chunks if i != index]
        if not _feasible(trial_chunks, chunk_options, core_locks):
            core_chunks = trial_chunks
    return core_locks, tuple(core_chunks)


# ----------------------------------------------------------------------
# main entry
# ----------------------------------------------------------------------
def solve_regional(
    *,
    chunks: Sequence[ChunkView],
    snapshot_revisions: Mapping[str, int],
    markers: Sequence[MarkerResolution],
    quotes: Sequence[RegionalQuote],
    locks: Mapping[str, int],
    region: str,
    terminology_version: int,
    max_concepts: int = MAX_CONCEPTS,
    max_usages: int = MAX_USAGES,
) -> RegionalPlan:
    """Solve one regional edition against one terminology version.

    ``markers`` are the resolutions of the specified terminology version;
    a marker applies to a chunk only when the segment revision it was
    indexed against matches the revision frozen in the base package.
    """

    for concept, value in sorted(locks.items()):
        if value < 1 or value > max_usages:
            raise InvalidRegionalLock(
                f"lock for concept {concept!r} has usage {value}, "
                f"outside 1..{max_usages}"
            )

    applicable: list[MarkerResolution] = [
        m
        for m in markers
        if snapshot_revisions.get(m.segment_id) == m.segment_revision
    ]
    concepts = {m.concept_id for m in applicable}
    if len(concepts) > max_concepts:
        raise RegionalProblemTooLarge(
            f"{len(concepts)} concepts involved, at most {max_concepts} supported"
        )

    quotes_by_chunk: dict[str, list[RegionalQuote]] = {}
    for quote in quotes:
        quotes_by_chunk.setdefault(quote.translation_id, []).append(quote)

    options_by_index: dict[int, list[DraftOption]] = {}
    rejected: list[tuple[str, str, str]] = []
    for index, chunk in enumerate(chunks):
        chunk_markers = [
            m for m in applicable if m.segment_id in chunk.source_ids
        ]
        if not chunk_markers:
            continue
        options, chunk_rejected = _chunk_options(
            chunk, chunk_markers, quotes_by_chunk.get(chunk.translation_id, []), max_usages
        )
        options_by_index[index] = options
        rejected.extend(chunk_rejected)

    # Any chunk with markers but no usable draft at all?
    starved = [
        index
        for index, chunk in enumerate(chunks)
        if any(m.segment_id in chunk.source_ids for m in applicable)
        and not options_by_index.get(index)
    ]

    locked_options: dict[int, list[DraftOption]] = {
        index: _lock_filter(options, locks) for index, options in options_by_index.items()
    }

    infeasible = bool(starved) or any(
        not locked_options.get(index)
        for index in range(len(chunks))
        if any(m.segment_id in chunks[index].source_ids for m in applicable)
    )
    if not infeasible:
        sweep_input: list[list[Optional[DraftOption]]] = []
        for index, chunk in enumerate(chunks):
            if index in options_by_index:
                sweep_input.append(locked_options[index])
            else:
                sweep_input.append([None])
        result = _sweep(sweep_input, optimize=True)
        infeasible = result is None
    else:
        result = None

    if infeasible:
        core_locks, core_chunks = _conflict_core(options_by_index, locks)
        conflicts: list[dict[str, object]] = [
            {"type": "lock", "concept": concept, "value": value}
            for concept, value in sorted(core_locks.items())
        ]
        for index in core_chunks:
            chunk = chunks[index]
            conflicts.append(
                {
                    "type": "chunk",
                    "translation_id": chunk.translation_id,
                    "source_ids": list(chunk.source_ids),
                    "options": [
                        {
                            "draft_id": option.draft_id,
                            "cost": option.cost,
                            "values": {c: v for c, v in option.concept_values},
                        }
                        for option in options_by_index.get(index, [])
                    ],
                }
            )
        if starved:
            reason = "no_usable_draft"
            empty = ", ".join(chunks[i].translation_id for i in starved)
            detail = (
                f"chunk(s) {empty} carry markers but have no usable draft; "
                "every draft was rejected or none was offered"
            )
        else:
            reason = "inconsistent_constraints"
            detail = (
                "no combination of drafts satisfies all locks and keeps every "
                "concept at a single value across the book; the listed "
                "constraints are mutually incompatible (removing any one of "
                "them makes the remainder feasible)"
            )
        return RegionalPlan(
            ok=False,
            region=region,
            terminology_version=terminology_version,
            choices=(),
            total_cost=None,
            concept_values=(),
            diagnosis=RegionalDiagnosis(
                reason=reason,
                detail=detail,
                conflicts=tuple(conflicts),
                rejected_drafts=tuple(rejected),
            ),
        )

    assert result is not None
    total_cost, _seq, chosen = result
    choices: list[RegionalChoice] = []
    concept_values: dict[str, int] = {}
    for chunk, option in zip(chunks, chosen):
        if option is None:
            choices.append(
                RegionalChoice(
                    translation_id=chunk.translation_id,
                    draft_id=None,
                    text=chunk.text,
                    assignments=(),
                    concept_values=(),
                )
            )
        else:
            choices.append(
                RegionalChoice(
                    translation_id=chunk.translation_id,
                    draft_id=option.draft_id,
                    text=option.text,
                    assignments=option.assignments,
                    concept_values=option.concept_values,
                )
            )
            for concept, value in option.concept_values:
                existing = concept_values.setdefault(concept, value)
                assert existing == value  # solver guarantees consistency
    return RegionalPlan(
        ok=True,
        region=region,
        terminology_version=terminology_version,
        choices=tuple(choices),
        total_cost=total_cost,
        concept_values=tuple(sorted(concept_values.items())),
        diagnosis=None,
    )
