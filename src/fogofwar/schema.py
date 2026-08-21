"""Bitemporal schema conventions for Fog of War.

HydraDB property values are integers, floats, booleans, and strings only —
no native timestamp type, no NULL (see cypher-compat.md: WHERE has no
IS NULL). Every timestamp in this schema is therefore an epoch-milliseconds
integer, and "still valid" is represented by an explicit open-ended
sentinel rather than NULL.

Every fact edge carries three timestamps, not one:

    t_commit       when this system durably recorded the fact
    t_valid_from   when the fact became true in the world
    t_valid_to     when the fact stopped being true (OPEN_END if still true)

t_commit is what point-in-time reconstruction filters on (see
query/pointintime.py) — it is the only honest way to answer "what did we
know as of session N" without leaking a later correction backward. Filtering
on t_valid_from instead would answer a different, wrong question.

Node labels
-----------
Session   one haystack session from a LongMemEval-style history
Turn      one user/assistant message within a session
Entity    a canonical thing a turn talks about (resolved by name, v1: exact
          string match — see extract.py for the upgrade path)

Relationship types
-------------------
CONTAINS   (Session)-[:CONTAINS {turn_index}]->(Turn)
MENTIONS   (Turn)-[:MENTIONS]->(Entity)
ASSERTS    (Entity)-[:ASSERTS {predicate, object, t_commit, t_valid_from,
           t_valid_to}]->(Entity) -- the bitemporal fact edge itself
"""

OPEN_END = 9_223_372_036_854_775_807  # i64::MAX -- "no end yet", not NULL


def stable_id(key: str) -> int:
    """Deterministic non-negative integer id from a stable string key.

    HydraDB node ids are non-negative integers, not arbitrary strings (see
    cypher-compat.md: "Node ids are non-negative integers"). Source data
    keys people actually, sanely, by string (session_id, entity name) --
    this maps any such key to a stable, reproducible integer so re-running
    ingestion MERGEs onto the same nodes instead of duplicating them.
    """
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def entity_pk(name: str) -> int:
    """The one canonical entity-name -> node-id mapping.

    Ingestion and every query MUST agree on this exact derivation
    (lowercase, strip, 'entity:' prefix) or reads silently miss nodes that
    writes created -- which is precisely the bug that motivated extracting
    this helper: the query side originally hashed the bare name while
    ingestion hashed the prefixed one, and every read came back empty.
    """
    return stable_id(f"entity:{name.strip().lower()}")

# HydraDB's Cypher subset: WHERE supports =, <>, <, >, <=, >=, STARTS WITH,
# combined with AND/OR/NOT -- no IN, no IS NULL. Filtering "still valid" is
# `t_valid_to > $asOf`, never `t_valid_to IS NULL`.


def epoch_millis(iso_or_date_str: str) -> int:
    """Parse a date string (LongMemEval's haystack_dates format) to epoch ms."""
    from datetime import datetime, timezone

    # LongMemEval haystack_dates are "YYYY/MM/DD (Day) HH:MM" in practice;
    # fall back through a couple of formats rather than assuming one.
    formats = [
        "%Y/%m/%d (%a) %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(iso_or_date_str.strip(), fmt).replace(
                tzinfo=timezone.utc
            )
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {iso_or_date_str!r}")


# --- Write templates -------------------------------------------------------
# Every batch write is UNWIND $rows AS row MERGE (...) SET ... -- the only
# documented batched-write shape (cypher-compat.md). One relationship type,
# one hop, per batch. In the vertex-upsert form the MERGE pattern may match
# on id ONLY -- labels go in SET (confirmed live: a label in the MERGE
# pattern fails with "UNWIND vertex upsert MERGE pattern matches only id;
# apply labels with SET").

UPSERT_SESSION = """
UNWIND $rows AS row
MERGE (s {id: row.id})
SET s:Session,
    s.session_key = row.session_key,
    s.question_id = row.question_id,
    s.order_index = row.order_index,
    s.t_commit = row.t_commit
"""

UPSERT_TURN = """
UNWIND $rows AS row
MERGE (t {id: row.id})
SET t:Turn,
    t.session_id = row.session_id,
    t.question_id = row.question_id,
    t.turn_index = row.turn_index,
    t.role = row.role,
    t.content = row.content,
    t.has_answer = row.has_answer,
    t.t_commit = row.t_commit
"""

LINK_SESSION_TURN = """
UNWIND $rows AS row
MATCH (s:Session {id: row.session_pk}), (t:Turn {id: row.turn_pk})
MERGE (s)-[r:CONTAINS {id: row.rel_id}]->(t)
SET r.turn_index = row.turn_index
"""

UPSERT_ENTITY = """
UNWIND $rows AS row
MERGE (e {id: row.id})
SET e:Entity, e.name = row.name
"""

LINK_TURN_ENTITY = """
UNWIND $rows AS row
MATCH (t:Turn {id: row.turn_pk}), (e:Entity {id: row.entity_pk})
MERGE (t)-[r:MENTIONS {id: row.rel_id}]->(e)
"""
