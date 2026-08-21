"""Point-in-time reconstruction -- the No-Leakage half of Fog of War.

The mechanism is one filter, on the field it's easy to get wrong: t_commit
(when we learned a fact), not t_valid_from (when it became true). Filtering
on t_valid_from lets a correction that was only entered in session 25 leak
backward into an honest answer about session 10, because a fact can become
true retroactively-on-paper (a backdated correction) without the asker
having had any way to know it yet. t_commit is the only field that encodes
"was this actually knowable at the time."
"""

from __future__ import annotations

from dataclasses import dataclass

from fogofwar.client import HydraDBClient
from fogofwar.schema import entity_pk

RECONSTRUCT_AS_OF = """
MATCH (e:Entity {id: $entityId})<-[:MENTIONS]-(t:Turn)
WHERE t.t_commit <= $asOf AND t.question_id = $questionId
RETURN t.id AS turn_id, t.content AS content, t.role AS role, t.t_commit AS t_commit
ORDER BY t.t_commit DESC
"""


@dataclass(frozen=True)
class TurnEvidence:
    turn_id: int
    content: str
    role: str
    t_commit: int


def reconstruct_as_of(
    client: HydraDBClient,
    entity_name: str,
    as_of_epoch_ms: int,
    question_id: str,
    consistency: str = "causal",
) -> list[TurnEvidence]:
    """Every turn mentioning `entity_name` that was knowable by `as_of_epoch_ms`.

    Scoped to one question's haystack: LongMemEval compiles an independent
    history per question, and entities are global nodes shared across all of
    them, so an unscoped read would mix evidence from unrelated instances
    (observed live before this filter existed -- duplicate turns from a
    previous ingest of the same content under a different question_id).

    Ordered most-recent-first, so the caller's "current as of this point"
    answer is evidence[0] if any evidence exists at all -- and if the list
    is empty, that's a `t_commit`-filtered zero, not "we didn't look hard
    enough."
    """
    entity_id = entity_pk(entity_name)
    rows = client.read(
        RECONSTRUCT_AS_OF,
        {"entityId": entity_id, "asOf": as_of_epoch_ms, "questionId": question_id},
        consistency=consistency,
    )
    return [
        TurnEvidence(
            turn_id=row["turn_id"],
            content=row["content"],
            role=row["role"],
            t_commit=row["t_commit"],
        )
        for row in rows
    ]
