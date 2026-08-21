# Architecture and formal claims

## The general property: point-in-time correctness under asynchronous ingestion

Fog of War is one instantiation of a property that applies to any bitemporal
graph fed by sources that don't share a clock: given facts arriving with a
`t_commit` (when durably recorded) and a `t_valid` interval (when true in
the world), a query answered "as of" some reference point R should satisfy:

**No-Leakage.** For a query posed with reference point R, the answer must
not depend on any fact whose `t_commit` is provably after R.

*Proposition.* A query that filters strictly on `t_commit <= R` (as
`query/pointintime.py`'s `RECONSTRUCT_AS_OF` does) satisfies No-Leakage by
construction: every row returned has `t_commit <= R` by the `WHERE` clause
itself, so no returned row can have a commit-time after R. *Proof:* immediate
from the semantics of `WHERE t.t_commit <= $asOf` — HydraDB evaluates this as
a boolean property comparison against a pinned snapshot (see HydraDB's own
architecture docs on snapshot-scoped reads), so the filter is exact, not
approximate.

This is a narrow, mechanically verifiable claim, deliberately. It says
nothing about whether the *right* facts exist to answer a question — only
that whichever facts are returned couldn't have leaked from the future. Two
failure modes remain in scope for evaluation, not eliminated by the
proposition above:

- **Extraction failure.** If `t_commit` itself is wrong (e.g., a turn was
  backdated, or the entity extraction in `extract.py` missed the mention
  entirely), the filter is exact over incorrect input. No-Leakage is a
  property of the query, not a guarantee about upstream data quality.
- **Recall failure.** No-Leakage says nothing about whether *enough*
  evidence is returned to actually answer the question — only that
  whatever is returned is honest.

## The narrower, honest claim on structural abstention

`query/abstention.py` checks a bounded co-occurrence pattern and treats zero
rows as a hard abstain. This is **not** a claim that graph traversal
replaces confidence-based retrieval — it's a routing decision over a
specific, checkable subset of questions.

`eval/metrics.py`'s `classify_structural_decidability` exists to measure
that subset empirically rather than assert its size: given a question set,
what fraction actually reduce to "do these two things co-occur in
something we were told"? The v1 classifier is a heuristic (needs two
capitalized-phrase entities in the question text) and is documented as an
*estimate*, not a certified number — see its docstring for exactly what it
gets wrong. Validating it against a manually labeled sample of
LongMemEval's abstention questions is the natural next step before citing
a decidability percentage anywhere.

## Ablation design (not yet run — this is the plan, not a result)

The claim that matters is which piece of the mechanism does the work, not
just that the whole system beats a strawman. Four conditions, crossed:

| | No temporal filter | `t_commit`-filtered |
|---|---|---|
| **No graph (flat list of turns)** | naive baseline (`eval/baseline.py`) | temporal filter alone |
| **Graph (entity co-occurrence)** | graph alone | full system |

Running all four against the same question set and reporting Leakage Rate
for each isolates whether the improvement comes from having a graph at
all, from the `t_commit` filter, or requires both together. Only the two
diagonal cells (naive baseline, full system) are implemented so far.

## Statistical honesty on small samples

LongMemEval-s has exactly 30 labeled abstention questions. Any accuracy
percentage computed on n=30 needs a confidence interval (Wilson score
interval for a proportion is the standard choice at this sample size) —
report it that way when these numbers are actually collected, not as a
bare percentage.

## Threat-model-equivalent scope note

This system assumes turn content and session ordering are honestly
reported by the source conversation — it does not defend against an
adversarial history (e.g., a session deliberately backdated to inject a
false "early" fact). That's a distinct problem from the one this repo
solves and is out of scope here.

## Limitations, stated plainly

- Entity extraction (`extract.py`) is a proper-noun regex heuristic, not
  NER or an LLM call. It under-extracts lowercase references and cannot
  resolve two different surface forms of the same entity ("Sam" vs. "S.
  Ratnaparkhi") — that's Parallax's problem, not this one, and deliberately
  not attempted here.
- Structural abstention only covers questions that reduce to an exact
  co-occurrence pattern; the actual fraction of real questions this covers
  is an open empirical question this repo can measure but hasn't yet.
- No results in this document are measured yet — every number mentioned
  is a metric definition or a plan, not a finding. See `README.md`'s
  status table for what's actually been run.
