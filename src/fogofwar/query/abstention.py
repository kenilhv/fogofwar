"""Structural abstention -- the routing decision from the v2 design.

Only applies to the slice of questions that reduce to "do these two things
co-occur in something we were told" -- a bounded graph pattern with a
checkable zero, not a similarity score sitting below some threshold. Every
other question keeps using confidence-based retrieval untouched; this
module has nothing to say about those and shouldn't be asked to.
"""

from __future__ import annotations

from dataclasses import dataclass

from fogofwar.client import HydraDBClient
from fogofwar.schema import entity_pk

CO_OCCURRENCE_CHECK = """
MATCH (subj:Entity {id: $subjectId})<-[:MENTIONS]-(t:Turn)-[:MENTIONS]->(obj:Entity {id: $objectId})
WHERE t.t_commit <= $asOf AND t.question_id = $questionId
RETURN t.id AS turn_id, t.content AS content, t.t_commit AS t_commit
ORDER BY t.t_commit DESC
"""


@dataclass(frozen=True)
class AbstentionResult:
    decidable: bool  # was this a structural pattern we could even check?
    should_abstain: bool
    evidence_turn_id: int | None
    evidence_content: str | None


def check_co_occurrence(
    client: HydraDBClient,
    subject_name: str,
    object_name: str,
    as_of_epoch_ms: int,
    question_id: str,
    consistency: str = "causal",
) -> AbstentionResult:
    """Checks whether any turn connects `subject_name` and `object_name`.

    Zero rows is a traversed, checked fact -- "we walked this exact pattern
    and it isn't there" -- not a low similarity score with no principled
    cutoff attached. `decidable` is always True here because a
    subject/object pair is exactly the shape this mechanism can evaluate;
    see eval/metrics.py for measuring what fraction of a real question set
    actually reduces to this shape in the first place.
    """
    subject_id = entity_pk(subject_name)
    object_id = entity_pk(object_name)
    rows = client.read(
        CO_OCCURRENCE_CHECK,
        {
            "subjectId": subject_id,
            "objectId": object_id,
            "asOf": as_of_epoch_ms,
            "questionId": question_id,
        },
        consistency=consistency,
    )
    if not rows:
        return AbstentionResult(
            decidable=True,
            should_abstain=True,
            evidence_turn_id=None,
            evidence_content=None,
        )
    top = rows[0]
    return AbstentionResult(
        decidable=True,
        should_abstain=False,
        evidence_turn_id=top["turn_id"],
        evidence_content=top["content"],
    )
