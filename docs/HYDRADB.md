# How HydraDB does the work

*Where to look, what it does, and what breaks without it.*

HydraDB is not vendored into this repository — it runs as a server and this project
talks to it over Bolt. So "where is HydraDB?" has a precise answer: **every fact this
system stores and every question it answers goes through HydraDB.** There is no
secondary store, no in-memory index, no cache holding results. Remove HydraDB and
nothing remains but transform code with nowhere to put its output.

This document maps that integration line by line.

---

## 1. The data path

```
LongMemEval JSON                    ingest/longmemeval.py   (pure transform, no I/O)
      |                                     |
      |  build_rows()                       v
      +-----------------------------> batched row dicts
                                            |
                                            v
                                   client.write_batch()      client.py
                                            |
                                     Bolt 5.x / bearer auth
                                            |
                                            v
                          +=========================================+
                          |            H Y D R A D B                |
                          |  Session / Turn / Entity vertices       |
                          |  CONTAINS / MENTIONS relationships      |
                          |  every t_commit, every t_valid interval |
                          +=========================================+
                                            |
                          causal reads              strong reads
                                            |
                      +---------------------+---------------------+
                      v                     v                     v
             pointintime.py          abstention.py           eval/*.py
          (No-Leakage query)      (structural check)       (baseline, flat)
                      |                     |                     |
                      +---------------------+---------------------+
                                            v
                                   demo_server.py -> demo UI
```

Nothing in that diagram bypasses the database. The demo server is a JSON shim over
the same query modules the evaluation uses; the UI holds no state of its own.

---

## 2. Every Cypher statement in the project

Twelve statements, all written inside HydraDB's documented OpenCypher subset and all
validated against the live parser.

### Writes — the batched `UNWIND … MERGE … SET` form

| Statement | Location | Purpose |
|---|---|---|
| `UPSERT_SESSION` | [`schema.py:100`](../src/fogofwar/schema.py) | Session vertices with `t_commit` |
| `UPSERT_TURN` | [`schema.py:110`](../src/fogofwar/schema.py) | Turn vertices carrying content + `t_commit` |
| `LINK_SESSION_TURN` | [`schema.py:123`](../src/fogofwar/schema.py) | `(Session)-[:CONTAINS]->(Turn)` |
| `UPSERT_ENTITY` | [`schema.py:130`](../src/fogofwar/schema.py) | Entity vertices |
| `LINK_TURN_ENTITY` | [`schema.py:136`](../src/fogofwar/schema.py) | `(Turn)-[:MENTIONS]->(Entity)` |

```cypher
UNWIND $rows AS row
MERGE (t {id: row.id})
SET t:Turn,
    t.session_id  = row.session_id,
    t.question_id = row.question_id,
    t.turn_index  = row.turn_index,
    t.role        = row.role,
    t.content     = row.content,
    t.has_answer  = row.has_answer,
    t.t_commit    = row.t_commit
```

Three HydraDB-specific constraints are visible in those eight lines, each learned from
a live parser rejection rather than guessed:

- The `UNWIND` input **must** be a parameter (`$rows`), never an inline list.
- The `MERGE` pattern may match on **id only** — the `:Turn` label is applied in `SET`.
  Writing `MERGE (t:Turn {id: row.id})` is rejected: *"UNWIND vertex upsert MERGE
  pattern matches only id; apply labels with SET."*
- Two rows in one batch that `SET` different values on the same vertex are rejected
  (*"conflicting metadata values"*), which is why ingestion de-duplicates every row
  type by primary key before sending.

### Reads

| Statement | Location | Purpose |
|---|---|---|
| `RECONSTRUCT_AS_OF` | [`query/pointintime.py:19`](../src/fogofwar/query/pointintime.py) | **The No-Leakage query** |
| `CO_OCCURRENCE_CHECK` | [`query/abstention.py:17`](../src/fogofwar/query/abstention.py) | **The abstention check** |
| `ALL_MENTIONS` | [`eval/baseline.py:16`](../src/fogofwar/eval/baseline.py) | Naive baseline (no temporal filter) |
| `ALL_QUESTION_TURNS`, `ALL_QUESTION_TURNS_FILTERED` | [`eval/flat.py`](../src/fogofwar/eval/flat.py) | No-graph ablation cells |
| `SESSION_BY_ID` | [`scripts/demo_server.py`](../scripts/demo_server.py) | Demo timeline |
| probe `MERGE` + `MATCH` | [`client.py`](../src/fogofwar/client.py) | Connection verification |

The two that carry the research claims:

```cypher
-- No-Leakage: evidence learned on or before the reference point, nothing later
MATCH (e:Entity {id: $entityId})<-[:MENTIONS]-(t:Turn)
WHERE t.t_commit <= $asOf AND t.question_id = $questionId
RETURN t.id AS turn_id, t.content AS content, t.role AS role, t.t_commit AS t_commit
ORDER BY t.t_commit DESC
```

```cypher
-- Structural abstention: zero rows is a checked fact, not a low score
MATCH (subj:Entity {id: $subjectId})<-[:MENTIONS]-(t:Turn)-[:MENTIONS]->(obj:Entity {id: $objectId})
WHERE t.t_commit <= $asOf AND t.question_id = $questionId
RETURN t.id AS turn_id, t.content AS content, t.t_commit AS t_commit
ORDER BY t.t_commit DESC
```

That second query is the whole abstention mechanism. It is a two-hop traversal through
a shared `Turn` vertex — the exact shape a graph engine exists to evaluate, and the
exact shape a vector index cannot express at all.

---

## 3. HydraDB primitives that are load-bearing

### Bitemporal edges — the reason this project uses HydraDB and not a vector store

HydraDB's own memory-layer write-up models a fact as
`e = (u, r, v, t_commit, t_valid, m)`. This project uses that formalism for a purpose
it was not originally proposed for: **guaranteeing a query answered "as of" a point in
a conversation cannot cite evidence the system learned later.**

`t_commit` (when the system learned it) and `t_valid` (when it was true) are different
axes, and the distinction is the entire contribution. Filtering on `t_valid` answers a
different — and, for this question, wrong — question: a backdated correction is
*valid* early but was not *knowable* early.

Because HydraDB has no native timestamp type and no `IS NULL`, the model is encoded as:

- epoch-millisecond integers ([`schema.py`](../src/fogofwar/schema.py), `epoch_millis`)
- an explicit open-interval sentinel `OPEN_END = i64::MAX` ([`schema.py:35`](../src/fogofwar/schema.py)) instead of NULL, so "still valid" is `t_valid_to > $asOf` rather than an `IS NULL` test the grammar does not support

### Integer-keyed identity

HydraDB node ids are non-negative integers, but source data keys naturally by string.
[`stable_id()`](../src/fogofwar/schema.py) (line 38) hashes any stable string key to a
deterministic non-negative int, so re-running ingestion `MERGE`s onto the same vertices
instead of duplicating them — which is what makes the whole ingest pipeline safely
resumable. [`entity_pk()`](../src/fogofwar/schema.py) (line 53) exists because ingest
and query sides once derived entity ids differently: writes succeeded, reads silently
returned nothing. One canonical derivation, one regression test.

### Consistency modes

Every read carries a mode ([`client.py:152`](../src/fogofwar/client.py)):

```python
session.run(query, parameters=params, hydradb_consistency=consistency)
```

`causal` for interactive polling (cheap, current durable view); `strong` reserved for a
result that must be provably fresh before being reported as final — HydraDB refreshes
the reader from object storage first, paying the freshness cost only where it matters.

### Idempotent writes as a reliability primitive

Because every write is `UNWIND … MERGE … SET`, retrying a failed batch is safe **by
construction**. [`_run_write_with_retry`](../src/fogofwar/client.py) (line 125) leans
on that with backoff, and `scripts/run_campaign.ps1` leans on it harder: ingestion runs
as resumable strides where a repeated stride is a no-op. That is a property of
HydraDB's MERGE semantics, not something layered on top.

---

## 4. Designing inside the documented subset

The Cypher subset is narrower than full OpenCypher, and every accommodation is
deliberate rather than a workaround:

| Constraint | How this project works within it |
|---|---|
| No `IN` | Batched writes use `UNWIND $rows`; multi-value reads iterate id lookups |
| No `IS NULL` | `OPEN_END` sentinel for open validity intervals |
| No `CONTAINS` | Substring matching happens client-side — and only in the *baseline* (`eval/flat.py`), which is the point: the graph path never needs it |
| One relationship type per pattern | Co-occurrence is expressed as two single-type hops through a shared `Turn` |
| One statement per request | Multi-step logic orchestrated in the query service, not fused into one statement |
| `CREATE` takes relationship paths only | The connection probe writes an edge, not a bare node |
| `WITH` is pass-through only | No aliasing or filtering mid-query |

---

## 5. Access patterns: what we learned about querying HydraDB well

The single most consequential performance lesson, learned three separate times:

> **Key by deterministic integer id. Do not filter on a non-indexed property.**

`MATCH (s:Session) WHERE s.question_id = $q` over ~24k Session vertices exceeds the
server's 30-second runtime budget. The same anti-pattern independently made the demo
timeline hang, made the 500-question evaluation take hours instead of minutes, and was
fixed the same way each time — resolve the id with `stable_id()` client-side, then do
an indexed lookup.

A second, subtler one: entity vertices are **global**, so a query anchored on a popular
entity expands that entity's *global* degree before the per-question filter applies.
Question-scoped entity keys would bound this; the trade-off (losing cross-question
entity identity) is documented but not taken.

Third: the v1 extractor promoted ultra-common capitalized words into entities — `"here"`
appears in **21.3% of all 246,750 corpus turns** — producing hub vertices with tens of
thousands of edges. [`compute_df_blocklist()`](../src/fogofwar/ingest/longmemeval.py)
(line 183) applies standard document-frequency pruning: an entity appearing in a large
fraction of all turns carries no discriminative value as evidence anyway.

---

## 6. Operating HydraDB at this scale

Ingesting the full benchmark (~258k turns, >1.2M edges) surfaced five reproducible
issues, all documented with repro steps in the [README](../README.md#findings-against-hydradb-v01-discovered-and-bisected-during-the-build).
The two that change how you must write client code:

- **Records over ~32KiB panic the write path** (bisected: 30,000 bytes fine, 32,767
  fails deterministically). Real chat data hits this, so ingestion caps content at
  [`MAX_CONTENT_BYTES`](../src/fogofwar/ingest/longmemeval.py) (line 35).
- **`DETACH DELETE` scans the entire cell**, so vertex deletion becomes unusable past
  ~1M edges. This project's connection probe originally cleaned up after itself and
  began failing purely because the database had grown; it is now MERGE-idempotent and
  never deletes.

Also required at this scale: `RUST_MIN_STACK=134217728` rather than the README's 32MB,
which segfaults the server under sustained write load.

---

## 7. The counterfactual

**What would break without HydraDB?**

Everything the project claims. Concretely:

- **The No-Leakage guarantee** is a `WHERE` clause over a commit-time property on
  persisted edges. A vector index stores embeddings and metadata, not a queryable
  temporal graph; the filter has nowhere to live and the property it guarantees cannot
  be stated.
- **Structural abstention** is a two-hop traversal whose *empty result* is the answer.
  Similarity search always returns a ranked list — it has no notion of "this pattern
  does not exist," which is precisely the fact the mechanism needs.
- **Resumable ingestion** of 500 haystacks depends on `MERGE` idempotency.
- **The measured 0.4254 baseline leakage** was produced by running both the filtered
  and unfiltered queries against the same persisted graph — the comparison requires a
  store that can answer both.

The one component that is not HydraDB-specific is entity extraction, which is
deliberately isolated in [`extract.py`](../src/fogofwar/extract.py) precisely so it can
be swapped without touching the graph layer.
