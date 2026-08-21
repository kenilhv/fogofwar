from fogofwar.eval.metrics import (
    classify_structural_decidability,
    leakage_rate,
    mean_leakage_rate,
    summarize_structural_decidability,
)


def test_leakage_rate_zero_when_all_evidence_before_reference():
    assert leakage_rate([100, 200, 300], reference_point=500) == 0.0


def test_leakage_rate_one_when_all_evidence_after_reference():
    assert leakage_rate([600, 700], reference_point=500) == 1.0


def test_leakage_rate_partial():
    # 1 of 4 pieces of evidence is after the reference point
    assert leakage_rate([100, 200, 300, 600], reference_point=500) == 0.25


def test_leakage_rate_empty_evidence_is_zero_not_error():
    assert leakage_rate([], reference_point=500) == 0.0


def test_leakage_rate_strict_inequality_at_boundary():
    # evidence committed exactly AT the reference point is not a leak
    assert leakage_rate([500], reference_point=500) == 0.0


def test_mean_leakage_rate_over_multiple_queries():
    assert mean_leakage_rate([0.0, 1.0, 0.5]) == 0.5


def test_mean_leakage_rate_empty_is_zero():
    assert mean_leakage_rate([]) == 0.0


def test_decidability_needs_two_entities():
    verdict = classify_structural_decidability("What's the sentiment on this project?")
    assert verdict.decidable is False


def test_decidability_true_with_two_entities():
    verdict = classify_structural_decidability("Does Sam Ratnaparkhi own Project Phoenix?")
    assert verdict.decidable is True
    assert verdict.candidate_subject == "Sam Ratnaparkhi"
    assert verdict.candidate_object == "Project Phoenix"


def test_decidability_summary_rate():
    questions = [
        "Does Bob own Project Phoenix?",  # decidable
        "What's the vibe on the team?",  # not decidable
        "Is Alice Chen the reviewer for AUTH-503?",  # decidable
    ]
    summary = summarize_structural_decidability(questions)
    assert summary.total == 3
    assert summary.decidable == 2
    assert summary.rate == 2 / 3


def test_wilson_interval_contains_point_estimate():
    from fogofwar.eval.metrics import wilson_interval

    low, high = wilson_interval(27, 30)
    assert low < 27 / 30 < high


def test_wilson_interval_is_wide_at_small_n():
    from fogofwar.eval.metrics import wilson_interval

    low, high = wilson_interval(27, 30)
    # 90% on n=30 is NOT a tight claim -- the 95% CI spans roughly 74%-97%
    assert high - low > 0.15


def test_wilson_interval_narrows_with_n():
    from fogofwar.eval.metrics import wilson_interval

    low_s, high_s = wilson_interval(27, 30)
    low_l, high_l = wilson_interval(900, 1000)
    assert (high_l - low_l) < (high_s - low_s)


def test_wilson_interval_zero_total_is_defined():
    from fogofwar.eval.metrics import wilson_interval

    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_interval_bounds_clamped():
    from fogofwar.eval.metrics import wilson_interval

    low, high = wilson_interval(30, 30)
    assert 0.0 <= low <= high <= 1.0
