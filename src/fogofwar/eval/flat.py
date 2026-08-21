"""Flat (non-graph) retrieval -- the "no-graph" row of the ablation 2x2.

Treats the store as a flat list of turns for a question and does client-side
substring matching, deliberately ignoring the MENTIONS graph structure. Its
job is to isolate what the graph contributes separately from what the
temporal filter contributes:

                     no temporal filter        t_commit-filtered
    no graph         flat_retrieve(asOf=None)  flat_retrieve(asOf=R)
    graph            eval/baseline.py          query/pointintime.py

If flat+filtered also hits zero leakage (it should -- the filter is doing
that work), that's an honest ablation finding: the graph's contribution is
evidence precision and structural abstention, not leakage elimination.

Substring matching happens client-side because HydraDB's WHERE has no
CONTAINS (cypher-compat.md) -- and that's fine: this cell is *supposed* to
represent the naive non-graph approach.
"""

from __future__ import annotations

from dataclasses import dataclass

from fogofwar.client import HydraDBClient

ALL_QUESTION_TURNS = """
MATCH (t:Turn)
WHERE t.question_id = $questionId
RETURN t.id AS turn_id, t.content AS content, t.t_commit AS t_commit
ORDER BY t.t_commit DESC
"""

ALL_QUESTION_TURNS_FILTERED = """
MATCH (t:Turn)
WHERE t.question_id = $questionId AND t.t_commit <= $asOf
RETURN t.id AS turn_id, t.content AS content, t.t_commit AS t_commit
ORDER BY t.t_commit DESC
"""


@dataclass(frozen=True)
class TurnEvidence:
    turn_id: int
    content: str
    t_commit: int


def flat_retrieve(
    client: HydraDBClient,
    entity_name: str,
    question_id: str,
    as_of_epoch_ms: int | None = None,
) -> list[TurnEvidence]:
    """Substring-matches `entity_name` over a question's turns, no graph.

    `as_of_epoch_ms=None` is the fully naive cell; a value fills the
    flat+filtered cell (the filter runs server-side on t_commit, matching
    the graph cells' semantics exactly, so the comparison is apples-to-
    apples on the one variable each cell changes).
    """
    if as_of_epoch_ms is None:
        rows = client.read(ALL_QUESTION_TURNS, {"questionId": question_id})
    else:
        rows = client.read(
            ALL_QUESTION_TURNS_FILTERED,
            {"questionId": question_id, "asOf": as_of_epoch_ms},
        )
    needle = entity_name.strip().lower()
    return [
        TurnEvidence(turn_id=r["turn_id"], content=r["content"], t_commit=r["t_commit"])
        for r in rows
        if needle in (r["content"] or "").lower()
    ]
