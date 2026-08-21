"""Abstention eval over LongMemEval-s's 30 labeled abstention questions.

LongMemEval marks a question as an abstention case with a question_id
ending `_abs`: the answer is genuinely not in the history, and the correct
behavior is to refuse. This measures whether the structural co-occurrence
check (query/abstention.py) actually refuses on those -- with the full
history knowable (as_of = last session), so refusal can't be an artifact
of the temporal filter hiding evidence.

Scope, stated honestly:
- Only questions where the v1 heuristic finds a subject/object pair are
  structurally decidable; the rest are reported as out of coverage, not
  silently dropped.
- The complementary number on answerable questions is a PROXY: for a
  non-_abs question, its top-2 entities co-occurring in some knowable turn
  does not prove that turn answers the question. It bounds false-abstention
  behavior, no more. Both numbers carry Wilson 95% intervals (n is small).

Run: python scripts/run_abstention_eval.py --data data/raw/longmemeval_s_cleaned.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fogofwar.client import HydraDBClient  # noqa: E402
from fogofwar.eval.metrics import (  # noqa: E402
    classify_structural_decidability,
    wilson_interval,
)
from fogofwar.query.abstention import check_co_occurrence  # noqa: E402
from fogofwar.schema import epoch_millis  # noqa: E402


def fmt_ci(successes: int, total: int) -> str:
    if total == 0:
        return "n/a (n=0)"
    low, high = wilson_interval(successes, total)
    return f"{successes}/{total} = {successes / total:.1%} (95% CI {low:.1%}-{high:.1%})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument(
        "--answerable-sample",
        type=int,
        default=60,
        help="How many answerable (non-_abs) questions to sample for the "
        "false-abstention proxy.",
    )
    args = parser.parse_args()

    instances = json.loads(args.data.read_text(encoding="utf-8"))
    abs_questions = [i for i in instances if i["question_id"].endswith("_abs")]
    answerable = [i for i in instances if not i["question_id"].endswith("_abs")]
    print(
        f"loaded {len(instances)} instances: {len(abs_questions)} abstention, "
        f"{len(answerable)} answerable",
        flush=True,
    )

    with HydraDBClient() as client:
        client.verify()

        def run_group(group: list[dict], label: str) -> tuple[int, int, int]:
            decidable = abstained = errors = 0
            for inst in group:
                verdict = classify_structural_decidability(inst["question"])
                if not verdict.decidable:
                    continue
                as_of = epoch_millis(inst["haystack_dates"][-1])
                try:
                    result = check_co_occurrence(
                        client,
                        verdict.candidate_subject,
                        verdict.candidate_object,
                        as_of,
                        inst["question_id"],
                    )
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    print(
                        f"  error on {inst['question_id']}: {type(exc).__name__}",
                        flush=True,
                    )
                    continue
                decidable += 1
                if result.should_abstain:
                    abstained += 1
            print(f"\n{label}:", flush=True)
            print(f"  structurally decidable: {decidable} (errors: {errors})", flush=True)
            return decidable, abstained, errors

        # --- the 30 labeled abstention questions ---
        n_abs, abstained_abs, _ = run_group(abs_questions, "abstention (_abs) questions")
        print(
            f"  correctly abstained: {fmt_ci(abstained_abs, n_abs)}",
            flush=True,
        )

        # --- answerable proxy sample ---
        sample = answerable[: args.answerable_sample]
        n_ans, abstained_ans, _ = run_group(sample, "answerable questions (proxy)")
        found = n_ans - abstained_ans
        print(
            f"  co-occurrence found (proxy for non-refusal): {fmt_ci(found, n_ans)}",
            flush=True,
        )
        print(
            "\nNote: the answerable-side number is a proxy -- co-occurrence of the "
            "question's top-2 entities does not prove the pair answers the "
            "question. See module docstring.",
            flush=True,
        )


if __name__ == "__main__":
    main()
