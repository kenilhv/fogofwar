"""End-to-end demo: ingest a slice of LongMemEval, run both mechanisms,
report Leakage Rate for the point-in-time query vs. the naive baseline.

Requires a running HydraDB node (see README.md "Getting started"). Run:
    python scripts/run_demo.py --data data/raw/longmemeval_oracle.json --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fogofwar.client import HydraDBClient, HydraDBConfig
from fogofwar.eval.baseline import naive_retrieve_all
from fogofwar.eval.flat import flat_retrieve
from fogofwar.eval.metrics import leakage_rate, mean_leakage_rate, summarize_structural_decidability
from fogofwar.extract import extract_entities
from fogofwar.ingest.longmemeval import compute_df_blocklist, ingest_instances
from fogofwar.query.pointintime import reconstruct_as_of
from fogofwar.schema import epoch_millis


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--ingest-chunk",
        type=int,
        default=25,
        help="Instances per ingest call. The full 277MB haystack file would "
        "otherwise build multi-GB row lists in one shot; chunking bounds "
        "peak memory. MERGE makes re-upserts across chunks idempotent.",
    )
    parser.add_argument(
        "--throttle-s",
        type=float,
        default=2.0,
        help="Pause between instance chunks during ingest. HydraDB's cache "
        "evictor queue was observed drowning (250k skipped events/30s) "
        "under unthrottled bulk write, ending in a query-executor panic; "
        "a short breather per chunk keeps the server inside its envelope.",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Evaluate against the already-populated graph without "
        "re-ingesting (a full ingest takes many minutes; re-running only "
        "the eval takes seconds).",
    )
    parser.add_argument(
        "--start-instance",
        type=int,
        default=0,
        help="Resume ingest from this instance index (0-based). Earlier "
        "instances are assumed already ingested (MERGE would make "
        "re-ingesting them harmless, just slow). Eval still runs over "
        "the full loaded set.",
    )
    parser.add_argument(
        "--end-instance",
        type=int,
        default=None,
        help="Stop ingest before this instance index. Lets a supervisor run "
        "ingestion in short strides, each in a fresh process, so a "
        "transient failure costs one stride rather than the whole run "
        "(see scripts/run_campaign.ps1).",
    )
    parser.add_argument(
        "--no-eval",
        action="store_true",
        help="Ingest only; skip the eval phase (for supervisor strides).",
    )
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="Run the full 2x2 (graph x temporal-filter) instead of just "
        "the two diagonal cells.",
    )
    args = parser.parse_args()

    instances = json.loads(args.data.read_text(encoding="utf-8"))[: args.limit]
    print(f"loaded {len(instances)} instance(s) from {args.data}", flush=True)

    with HydraDBClient(HydraDBConfig()) as client:
        print("verifying HydraDB connection...", flush=True)
        client.verify()
        print("connection OK", flush=True)

        if args.skip_ingest:
            print("skipping ingest (using existing graph)", flush=True)
        else:
            import time as _time

            blocklist = compute_df_blocklist(instances)
            print(
                f"DF blocklist: {len(blocklist)} hub entities pruned "
                f"(> 1% turn frequency)",
                flush=True,
            )

            totals: dict[str, int] = {}
            ingest_end = (
                len(instances)
                if args.end_instance is None
                else min(args.end_instance, len(instances))
            )
            for start in range(args.start_instance, ingest_end, args.ingest_chunk):
                chunk = instances[start : min(start + args.ingest_chunk, ingest_end)]
                counts = ingest_instances(client, chunk, entity_blocklist=blocklist)
                for key, value in counts.items():
                    totals[key] = totals.get(key, 0) + value
                print(
                    f"  ingested instances {start + 1}-{start + len(chunk)}"
                    f" of {len(instances)} (running totals: {totals})",
                    flush=True,
                )
                if args.throttle_s > 0 and start + args.ingest_chunk < len(instances):
                    _time.sleep(args.throttle_s)
            print(f"ingested: {totals}", flush=True)

        if args.no_eval:
            print("skipping eval (--no-eval)", flush=True)
            return

        # --- Leakage Rate across the (graph x temporal-filter) grid ---
        cells: dict[str, list[float]] = {
            "graph+filter": [],
            "graph": [],
            "flat+filter": [],
            "flat": [],
        }

        eval_errors = 0
        for instance in instances:
            question = instance["question"]
            entities = extract_entities(question)
            if not entities:
                continue
            subject = entities[0]
            question_id = instance["question_id"]

            # reference point: MID-history, deliberately. Anchoring at the
            # last session would make leakage impossible for the naive
            # baseline too (nothing exists after it), turning the comparison
            # vacuous. Posing the question at the midpoint means the second
            # half of the history exists-but-must-not-be-used -- the exact
            # condition Leakage Rate measures.
            # A read touching a pre-existing junk-hub entity (ingested before
            # DF pruning existed) can hit the same admission-control scan
            # limit that motivated the pruning; one bad question must not
            # kill the whole eval.
            try:
                # as_of comes straight from the instance's own haystack_dates
                # -- the exact value ingestion wrote as that session's
                # t_commit -- rather than a per-question session_t_commit DB
                # lookup. That lookup label-scans ~24k Session vertices per
                # call (no index on question_id) and single-handedly turned
                # the 500-question eval from minutes into hours.
                mid_index = (len(instance["haystack_session_ids"]) - 1) // 2
                as_of = epoch_millis(instance["haystack_dates"][mid_index])

                correct_evidence = reconstruct_as_of(client, subject, as_of, question_id)
                naive_evidence = naive_retrieve_all(client, subject, question_id)
                graph_filter_rate = leakage_rate(
                    [e.t_commit for e in correct_evidence], as_of
                )
                graph_rate = leakage_rate([e.t_commit for e in naive_evidence], as_of)

                flat_filter_rate = flat_rate = None
                if args.ablation:
                    flat_filtered = flat_retrieve(client, subject, question_id, as_of)
                    flat_naive = flat_retrieve(client, subject, question_id, None)
                    flat_filter_rate = leakage_rate(
                        [e.t_commit for e in flat_filtered], as_of
                    )
                    flat_rate = leakage_rate([e.t_commit for e in flat_naive], as_of)
            except Exception as exc:  # noqa: BLE001 -- log-and-skip is the point
                eval_errors += 1
                print(f"  eval error on {question_id}: {type(exc).__name__}", flush=True)
                continue
            # all-or-nothing append keeps the four cell lists in lockstep
            cells["graph+filter"].append(graph_filter_rate)
            cells["graph"].append(graph_rate)
            if args.ablation:
                cells["flat+filter"].append(flat_filter_rate)
                cells["flat"].append(flat_rate)

        print(f"\nqueries evaluated: {len(cells['graph+filter'])} (errors skipped: {eval_errors})")
        print(f"mean leakage rate, point-in-time query (graph+filter): {mean_leakage_rate(cells['graph+filter']):.4f}")
        print(f"mean leakage rate, naive graph baseline (no filter):   {mean_leakage_rate(cells['graph']):.4f}")
        if args.ablation:
            print(f"mean leakage rate, flat substring + t_commit filter:   {mean_leakage_rate(cells['flat+filter']):.4f}")
            print(f"mean leakage rate, flat substring, no filter:          {mean_leakage_rate(cells['flat']):.4f}")

        # --- Structural Decidability, over the loaded question set ---
        questions = [i["question"] for i in instances]
        summary = summarize_structural_decidability(questions)
        print(
            f"\nstructural decidability (v1 heuristic): "
            f"{summary.decidable}/{summary.total} ({summary.rate:.1%})"
        )


if __name__ == "__main__":
    main()
