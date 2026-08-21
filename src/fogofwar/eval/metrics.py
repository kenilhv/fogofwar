"""Leakage Rate and Structural Decidability -- the two measurable claims.

Both are defined precisely enough to compute, not just gesture at. Neither
needs a live HydraDB node to run against a list of (evidence, reference
point) pairs already pulled from somewhere -- these are pure functions over
data, kept separate from the query layer that produces that data.
"""

from __future__ import annotations

from dataclasses import dataclass

from fogofwar.extract import extract_entities


def leakage_rate(evidence_t_commits: list[int], reference_point: int) -> float:
    """Fraction of cited evidence with t_commit strictly after `reference_point`.

    0.0 means every piece of evidence used to answer was actually knowable
    as of the reference point -- the property a t_commit-filtered query
    satisfies by construction (see query/pointintime.py). Undefined (and
    reported as 0.0, not an error) when there's no evidence at all, since
    an empty citation set can't leak anything.
    """
    if not evidence_t_commits:
        return 0.0
    leaked = sum(1 for t in evidence_t_commits if t > reference_point)
    return leaked / len(evidence_t_commits)


def mean_leakage_rate(per_query_rates: list[float]) -> float:
    if not per_query_rates:
        return 0.0
    return sum(per_query_rates) / len(per_query_rates)


@dataclass(frozen=True)
class DecidabilityVerdict:
    decidable: bool
    candidate_subject: str | None
    candidate_object: str | None
    reason: str


def classify_structural_decidability(question_text: str) -> DecidabilityVerdict:
    """Heuristic v1 classifier: is this question shaped like an exact co-occurrence check?

    Needs at least two distinct proper-noun-style entities in the question
    itself to even have a subject/object pair to check -- "did X do Y" has
    two; "what's the sentiment on this project" has none. This is a coarse,
    honest stand-in for what a real annotation pass (or an LLM classifier)
    would do; it will under-count decidable questions phrased without
    capitalized entities and over-count ones that happen to name two
    proper nouns without actually being a relational check. Report the
    aggregate rate as an estimate, not a certified number, until validated
    against a manually labeled sample.
    """
    entities = extract_entities(question_text)
    if len(entities) < 2:
        return DecidabilityVerdict(
            decidable=False,
            candidate_subject=None,
            candidate_object=None,
            reason=f"only {len(entities)} candidate entit(y/ies) found, need >= 2",
        )
    return DecidabilityVerdict(
        decidable=True,
        candidate_subject=entities[0],
        candidate_object=entities[1],
        reason="two or more candidate entities found",
    )


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion -- the standard choice at
    small n, where the naive normal approximation misbehaves.

    Any percentage this project reports on LongMemEval's 30 abstention
    questions (or any similarly small slice) must carry this interval, not
    stand as a bare number: 27/30 vs 21/30 without an interval is
    uninterpretable as signal vs. noise. z=1.96 gives a 95% interval.
    """
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    margin = (z / denom) * ((p * (1 - p) / total + z**2 / (4 * total**2)) ** 0.5)
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass(frozen=True)
class DecidabilitySummary:
    total: int
    decidable: int

    @property
    def rate(self) -> float:
        return self.decidable / self.total if self.total else 0.0

    @property
    def rate_ci95(self) -> tuple[float, float]:
        return wilson_interval(self.decidable, self.total)


def summarize_structural_decidability(question_texts: list[str]) -> DecidabilitySummary:
    verdicts = [classify_structural_decidability(q) for q in question_texts]
    decidable = sum(1 for v in verdicts if v.decidable)
    return DecidabilitySummary(total=len(question_texts), decidable=decidable)
