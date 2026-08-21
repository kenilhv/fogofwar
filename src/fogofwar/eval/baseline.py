"""Naive baseline: retrieve every turn mentioning an entity, no temporal filter.

Stands in for "plain retrieval over the full unrolled history" -- the thing
most memory systems actually do. Its only job is to have a nonzero Leakage
Rate to compare against, so the point-in-time-correct query's zero means
something instead of being an uncontested claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from fogofwar.client import HydraDBClient
from fogofwar.schema import entity_pk

ALL_MENTIONS = """
MATCH (e:Entity {id: $entityId})<-[:MENTIONS]-(t:Turn)
WHERE t.question_id = $questionId
RETURN t.id AS turn_id, t.content AS content, t.t_commit AS t_commit
ORDER BY t.t_commit DESC
"""


@dataclass(frozen=True)
class TurnEvidence:
    turn_id: int
    content: str
    t_commit: int


def naive_retrieve_all(
    client: HydraDBClient, entity_name: str, question_id: str
) -> list[TurnEvidence]:
    """Every turn mentioning `entity_name`, regardless of when it was learned.

    Question-scoped like the point-in-time query, so the comparison isolates
    the temporal filter -- the one variable under test -- rather than
    conflating it with cross-question contamination.
    """
    entity_id = entity_pk(entity_name)
    rows = client.read(ALL_MENTIONS, {"entityId": entity_id, "questionId": question_id})
    return [
        TurnEvidence(turn_id=r["turn_id"], content=r["content"], t_commit=r["t_commit"])
        for r in rows
    ]
