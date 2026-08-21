from fogofwar.ingest.longmemeval import build_rows
from fogofwar.schema import OPEN_END, stable_id

SAMPLE_INSTANCE = {
    "question_id": "q1",
    "question": "Who owns the migration now?",
    "haystack_session_ids": ["sess-a", "sess-b"],
    "haystack_dates": ["2026/03/12 (Thu) 09:00", "2026/07/01 (Wed) 14:30"],
    "haystack_sessions": [
        [
            {"role": "user", "content": "Assign the Ontology Migration to Priya Shah."},
            {"role": "assistant", "content": "Done, Priya Shah now owns it.", "has_answer": True},
        ],
        [
            {"role": "user", "content": "Priya Shah is off the migration, Tom Chen has it now."},
        ],
    ],
}


def test_build_rows_produces_one_session_per_haystack_entry():
    rows = build_rows([SAMPLE_INSTANCE])
    assert len(rows.sessions) == 2
    assert rows.sessions[0]["order_index"] == 0
    assert rows.sessions[1]["order_index"] == 1


def test_sessions_are_scoped_per_question():
    # same raw session_id, different question -- must not collide
    other_instance = dict(SAMPLE_INSTANCE, question_id="q2")
    rows = build_rows([SAMPLE_INSTANCE, other_instance])
    session_ids = {s["id"] for s in rows.sessions}
    assert len(session_ids) == 4  # 2 sessions x 2 questions, all distinct


def test_turn_t_commit_matches_parent_session():
    rows = build_rows([SAMPLE_INSTANCE])
    session_0_commit = rows.sessions[0]["t_commit"]
    turns_in_session_0 = [t for t in rows.turns if t["session_id"] == rows.sessions[0]["session_key"]]
    assert len(turns_in_session_0) == 2
    assert all(t["t_commit"] == session_0_commit for t in turns_in_session_0)


def test_earlier_session_has_smaller_t_commit():
    rows = build_rows([SAMPLE_INSTANCE])
    assert rows.sessions[0]["t_commit"] < rows.sessions[1]["t_commit"]


def test_has_answer_flag_preserved():
    rows = build_rows([SAMPLE_INSTANCE])
    flagged = [t for t in rows.turns if t["has_answer"]]
    assert len(flagged) == 1
    assert "Priya Shah now owns it" in flagged[0]["content"]


def test_entity_extraction_creates_priya_and_tom():
    rows = build_rows([SAMPLE_INSTANCE])
    names = {e["name"] for e in rows.entities}
    assert "Priya Shah" in names
    assert "Tom Chen" in names


def test_stable_id_is_deterministic_across_calls():
    assert stable_id("entity:priya shah") == stable_id("entity:priya shah")


def test_stable_id_is_nonnegative():
    assert stable_id("anything") >= 0
    assert stable_id("anything") <= OPEN_END


def test_repeated_ingest_of_same_instance_is_idempotent_at_row_level():
    # MERGE makes duplicate rows harmless server-side; at minimum the pk
    # for the same logical session must be identical across two builds.
    rows_a = build_rows([SAMPLE_INSTANCE])
    rows_b = build_rows([SAMPLE_INSTANCE])
    assert [s["id"] for s in rows_a.sessions] == [s["id"] for s in rows_b.sessions]


def test_entity_pk_agrees_between_ingest_and_query_side():
    """Regression: ingest and query must derive the same entity node id.

    The original bug: ingestion hashed 'entity:<name>' while queries hashed
    the bare name -- every read silently returned zero rows against a
    correctly populated graph.
    """
    from fogofwar.schema import entity_pk

    rows = build_rows([SAMPLE_INSTANCE])
    ingested_ids = {e["id"] for e in rows.entities}
    assert entity_pk("Priya Shah") in ingested_ids
    assert entity_pk("  priya shah  ") in ingested_ids  # normalization agrees too


def test_case_variant_entities_dedupe_to_one_row():
    """Regression: two case-variants of one entity in one batch must produce
    exactly one entity row -- HydraDB rejects conflicting SET values for the
    same vertex within an UNWIND batch ("conflicting metadata values")."""
    instance = dict(
        SAMPLE_INSTANCE,
        question_id="q-case",
        haystack_sessions=[
            [
                {"role": "user", "content": "GARMIN shipped the update."},
                {"role": "user", "content": "I like Garmin a lot."},
            ],
        ],
        haystack_session_ids=["sess-x"],
        haystack_dates=["2026/01/05 (Mon) 10:00"],
    )
    rows = build_rows([instance])
    from fogofwar.schema import entity_pk

    garmin_rows = [e for e in rows.entities if e["id"] == entity_pk("garmin")]
    assert len(garmin_rows) == 1


def test_duplicate_session_in_one_haystack_first_occurrence_wins():
    """Regression: longmemeval_s_cleaned haystacks can repeat the same
    session id at two positions. Two rows with different order_index for one
    session vertex in one batch fail live with 'conflicting metadata values
    ... property order_index' -- first occurrence must win, and the
    duplicate must not re-emit its turns or links."""
    instance = dict(
        SAMPLE_INSTANCE,
        question_id="q-dup",
        haystack_session_ids=["sess-r", "sess-other", "sess-r"],
        haystack_dates=[
            "2026/01/05 (Mon) 10:00",
            "2026/02/06 (Fri) 11:00",
            "2026/03/07 (Sat) 12:00",
        ],
        haystack_sessions=[
            [{"role": "user", "content": "Alpha Team owns Beta Project."}],
            [{"role": "user", "content": "Unrelated chatter."}],
            [{"role": "user", "content": "Alpha Team owns Beta Project."}],
        ],
    )
    rows = build_rows([instance])
    session_pks = [s["id"] for s in rows.sessions]
    assert len(session_pks) == len(set(session_pks)) == 2
    kept = [s for s in rows.sessions if s["session_key"] == "q-dup:sess-r"]
    assert len(kept) == 1
    assert kept[0]["order_index"] == 0  # first occurrence, not the repeat
    turn_pks = [t["id"] for t in rows.turns]
    assert len(turn_pks) == len(set(turn_pks)) == 2


def test_oversized_turn_content_is_capped_below_record_limit():
    """Regression: contents >= ~32KB deterministically panic HydraDB's write
    path (slatedb batch.rs:154, bisected live: 30,000 OK / 32,767 fails).
    Ingest must cap content below the threshold, with a visible marker."""
    from fogofwar.ingest.longmemeval import MAX_CONTENT_BYTES, _TRUNCATION_MARKER

    instance = dict(
        SAMPLE_INSTANCE,
        question_id="q-big",
        haystack_session_ids=["sess-big"],
        haystack_dates=["2026/01/05 (Mon) 10:00"],
        haystack_sessions=[[{"role": "user", "content": "Alpha " + "x" * 45_000}]],
    )
    rows = build_rows([instance])
    content = rows.turns[0]["content"]
    assert content.endswith(_TRUNCATION_MARKER)
    assert len(content.encode("utf-8")) <= MAX_CONTENT_BYTES + len(
        _TRUNCATION_MARKER.encode("utf-8")
    )


def test_entity_blocklist_prunes_hub_entities():
    """Regression: ultra-common capitalized words ('here' = 21% of all corpus
    turns) become mega-hub entity vertices whose relationship MERGEs exceed
    HydraDB's admission-control scan limit (observed live: 'actual 1000001
    exceeds limit 1000000'). DF-blocklisted names must produce no entity or
    mention rows."""
    rows = build_rows([SAMPLE_INSTANCE], entity_blocklist=frozenset(["priya shah"]))
    names = {e["name"].lower() for e in rows.entities}
    assert "priya shah" not in names
    assert "tom chen" in names  # non-blocked entities unaffected
