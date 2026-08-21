"""Transforms LongMemEval-format instances into HydraDB batch-write rows.

Kept as pure functions (JSON in, row-dicts out) deliberately -- the
transform logic is the part worth unit-testing without a live HydraDB node,
so `build_rows` has no I/O in it at all. `ingest_instances` is the thin
orchestration layer that actually writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fogofwar.client import HydraDBClient
from fogofwar.extract import extract_entities
from fogofwar.schema import (
    LINK_SESSION_TURN,
    LINK_TURN_ENTITY,
    UPSERT_ENTITY,
    UPSERT_SESSION,
    UPSERT_TURN,
    entity_pk,
    epoch_millis,
    stable_id,
)


# HydraDB deterministically panics its write path ("corrupt value at
# client/query/executor", slatedb batch.rs:154) when a record's encoded size
# crosses ~32KiB -- bisected live: a 30,000-byte content property writes
# fine, 32,767 bytes fails every time, and the failure is per-record, not
# load-related. Cap content below the threshold with margin for the row's
# other properties. Truncation affects evidence display text only; t_commit
# and the graph structure -- everything the metrics depend on -- are intact.
MAX_CONTENT_BYTES = 28_000
_TRUNCATION_MARKER = " [truncated: exceeds HydraDB ~32KiB record limit]"


def _cap_content(content: str) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= MAX_CONTENT_BYTES:
        return content
    # cut on a UTF-8 boundary, then append the marker
    cut = encoded[:MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
    return cut + _TRUNCATION_MARKER


@dataclass
class IngestRows:
    sessions: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    session_turn_links: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    turn_entity_links: list[dict] = field(default_factory=list)


def build_rows(
    instances: list[dict[str, Any]],
    entity_blocklist: frozenset[str] = frozenset(),
) -> IngestRows:
    """Transforms a list of LongMemEval instances into batch-write rows.

    Each instance contributes: question_id, haystack_session_ids,
    haystack_dates, haystack_sessions (list of turn-lists, each turn a
    {"role", "content", optional "has_answer"} dict). Sessions are scoped
    per-question (`session_key = f"{question_id}:{session_id}"`) because
    LongMemEval compiles a distinct haystack per question and the same raw
    session_id can otherwise collide across unrelated instances.

    `entity_blocklist` (casefolded names) drops entity + mention rows for
    pathological hub entities. The v1 extractor promotes ultra-common
    sentence-initial words ('here' appears in 21% of all corpus turns) into
    entities; globally-shared entity nodes then accumulate tens of
    thousands of MENTIONS edges each, and a relationship MERGE touching
    such a hub eventually exceeds HydraDB's admission-control scan limit
    ("actual 1000001 exceeds limit 1000000" -- observed live, deterministic
    at 3/3 attempts once the graph was big enough). Document-frequency
    pruning is the standard IR answer: an entity appearing in a large
    fraction of all turns has no discriminative value as evidence anyway.
    """
    rows = IngestRows()
    # Dedupe registries -- every row type, by its primary key, first-seen
    # wins. HydraDB rejects two rows SETting different values on one vertex
    # within one UNWIND batch ("conflicting metadata values"), and the data
    # actually produces such collisions two distinct ways, both observed
    # live: (1) case-variant entities -- "GARMIN"/"Garmin" hash to one node
    # id but carry different `name` values; (2) duplicate session ids within
    # one question's haystack in longmemeval_s_cleaned -- the same session
    # sampled at two positions, yielding two different `order_index` (and
    # potentially t_commit) values for one session vertex, and identical
    # collisions for every turn inside it.
    seen_session_pks: set[int] = set()
    seen_turn_pks: set[int] = set()
    seen_contains_rel_ids: set[int] = set()
    seen_entity_pks: set[int] = set()
    seen_mention_rel_ids: set[int] = set()

    for instance in instances:
        question_id = instance["question_id"]
        session_ids = instance["haystack_session_ids"]
        dates = instance["haystack_dates"]
        sessions = instance["haystack_sessions"]

        for order_index, (session_id, date_str, turns) in enumerate(
            zip(session_ids, dates, sessions)
        ):
            session_key = f"{question_id}:{session_id}"
            session_pk = stable_id(session_key)
            t_commit = epoch_millis(date_str)

            if session_pk in seen_session_pks:
                # Same session repeated at another haystack position --
                # first occurrence (and its order_index/t_commit) wins;
                # its turns and links are already built.
                continue
            seen_session_pks.add(session_pk)

            rows.sessions.append(
                {
                    "id": session_pk,
                    "session_key": session_key,
                    "question_id": question_id,
                    "order_index": order_index,
                    "t_commit": t_commit,
                }
            )

            for turn_index, turn in enumerate(turns):
                turn_key = f"{session_key}:{turn_index}"
                turn_pk = stable_id(turn_key)
                content = _cap_content(turn.get("content", ""))

                if turn_pk in seen_turn_pks:
                    continue
                seen_turn_pks.add(turn_pk)

                rows.turns.append(
                    {
                        "id": turn_pk,
                        "session_id": session_key,
                        "question_id": question_id,
                        "turn_index": turn_index,
                        "role": turn.get("role", "unknown"),
                        "content": content,
                        "has_answer": bool(turn.get("has_answer", False)),
                        "t_commit": t_commit,
                    }
                )
                contains_rel_id = stable_id(f"{turn_key}:contains")
                if contains_rel_id not in seen_contains_rel_ids:
                    seen_contains_rel_ids.add(contains_rel_id)
                    rows.session_turn_links.append(
                        {
                            "session_pk": session_pk,
                            "turn_pk": turn_pk,
                            "turn_index": turn_index,
                            "rel_id": contains_rel_id,
                        }
                    )

                for entity_name in extract_entities(content):
                    entity_key = entity_name.strip().lower()
                    if entity_key in entity_blocklist:
                        continue
                    pk = entity_pk(entity_name)
                    if pk not in seen_entity_pks:
                        seen_entity_pks.add(pk)
                        rows.entities.append({"id": pk, "name": entity_name})
                    rel_id = stable_id(f"{turn_key}:mentions:{entity_key}")
                    if rel_id not in seen_mention_rel_ids:
                        seen_mention_rel_ids.add(rel_id)
                        rows.turn_entity_links.append(
                            {
                                "turn_pk": turn_pk,
                                "entity_pk": pk,
                                "rel_id": rel_id,
                            }
                        )

    return rows


def compute_df_blocklist(
    instances: list[dict[str, Any]], max_df_fraction: float = 0.01
) -> frozenset[str]:
    """Entities appearing in more than `max_df_fraction` of all turns.

    Computed over the full loaded corpus (not per chunk) so the threshold
    means the same thing regardless of ingest stride boundaries.
    """
    import collections

    freq: collections.Counter[str] = collections.Counter()
    total_turns = 0
    for instance in instances:
        for session in instance["haystack_sessions"]:
            for turn in session:
                total_turns += 1
                names = {
                    e.strip().lower()
                    for e in extract_entities(turn.get("content") or "")
                }
                freq.update(names)
    cutoff = max_df_fraction * total_turns
    return frozenset(name for name, n in freq.items() if n > cutoff)


def ingest_instances(
    client: HydraDBClient,
    instances: list[dict[str, Any]],
    entity_blocklist: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Transforms and writes a batch of LongMemEval instances. Returns counts."""
    rows = build_rows(instances, entity_blocklist=entity_blocklist)
    counts = {
        "sessions": client.write_batch(UPSERT_SESSION, rows.sessions),
        "turns": client.write_batch(UPSERT_TURN, rows.turns),
        "session_turn_links": client.write_batch(
            LINK_SESSION_TURN, rows.session_turn_links
        ),
        "entities": client.write_batch(UPSERT_ENTITY, rows.entities),
        "turn_entity_links": client.write_batch(
            LINK_TURN_ENTITY, rows.turn_entity_links
        ),
    }
    return counts
