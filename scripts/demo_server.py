"""Demo server for the Fog of War UI.

Every number the UI shows comes over Bolt from the live HydraDB node --
this server is a thin JSON shim over the same query modules the eval uses
(query/pointintime.py, query/abstention.py, eval/baseline.py). Nothing is
canned; switching the UI to "naive" mode really does run the unfiltered
query and really does return future evidence.

stdlib http.server on purpose: zero new dependencies, and the demo's whole
credibility rests on the queries being the project's real ones, not on the
web layer.

Run:  python scripts/demo_server.py   (HydraDB must be up on 127.0.0.1:7687)
Then: http://127.0.0.1:8377
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fogofwar.client import HydraDBClient  # noqa: E402
from fogofwar.eval.baseline import naive_retrieve_all  # noqa: E402
from fogofwar.ingest.longmemeval import ingest_instances  # noqa: E402
from fogofwar.query.abstention import check_co_occurrence  # noqa: E402
from fogofwar.query.pointintime import reconstruct_as_of  # noqa: E402
from fogofwar.schema import stable_id  # noqa: E402

PORT = 8377

# The ownership-change scenario: the clearest 60-second story. Ingested at
# startup (idempotent MERGE), then queried live like everything else.
DEMO_INSTANCE = {
    "question_id": "demo-ownership",
    "question": "Who owns the Ontology Migration?",
    "haystack_session_ids": ["s-mar", "s-may", "s-jul"],
    "haystack_dates": [
        "2026/03/12 (Thu) 09:00",
        "2026/05/02 (Sat) 11:30",
        "2026/07/01 (Wed) 14:30",
    ],
    "haystack_sessions": [
        [
            {"role": "user", "content": "Assign the Ontology Migration to Priya Shah."},
            {
                "role": "assistant",
                "content": "Done. Priya Shah owns the Ontology Migration now.",
                "has_answer": True,
            },
        ],
        [
            {
                "role": "user",
                "content": "Status check: Priya Shah says the Ontology Migration is blocked on AUTH-503.",
            },
        ],
        [
            {
                "role": "user",
                "content": "Priya Shah is off it. Tom Chen owns the Ontology Migration now.",
            },
        ],
    ],
}

# One real benchmark question alongside the constructed scenario, so the
# demo provably runs on LongMemEval data too. Chosen because its subject
# ("Spotify") extracts cleanly and its evidence spans sessions.
#
# Its session ids and dates are extracted offline into
# demo/real_scenario.json so this server never parses the 277MB benchmark
# file. To regenerate that file for a different question id:
#
#   python -c "import json; d=json.load(open('data/raw/longmemeval_s_cleaned.json',encoding='utf-8')); i=next(x for x in d if x['question_id']=='1e043500'); json.dump({'question_id':i['question_id'],'question':i['question'],'session_ids':i['haystack_session_ids'],'dates':i['haystack_dates']}, open('demo/real_scenario.json','w',encoding='utf-8'))"
_REAL = json.loads((ROOT / "demo" / "real_scenario.json").read_text(encoding="utf-8"))
REAL_QUESTION_ID = _REAL["question_id"]
REAL_QUESTION_TEXT = _REAL["question"]
REAL_SUBJECT = "Spotify"

SESSION_KEYS = {
    "demo-ownership": [
        f"demo-ownership:{sid}" for sid in DEMO_INSTANCE["haystack_session_ids"]
    ],
    REAL_QUESTION_ID: [
        f"{REAL_QUESTION_ID}:{sid}" for sid in _REAL["session_ids"]
    ],
}

# Timeline lookups go BY SESSION ID, never by label scan: session ids are
# deterministic (stable_id(f"{qid}:{sid}")), and an id lookup is indexed
# while `MATCH (s:Session) WHERE s.question_id = ...` label-scans ~24k
# vertices and times out against the server's runtime budget (observed
# live -- 30s+ with no rows). Same lesson as the rest of the project: use
# the id-keyed access path.
SESSION_BY_ID = """
MATCH (s:Session {id: $sessionPk})
RETURN s.id AS id, s.order_index AS order_index, s.t_commit AS t_commit
"""

SCENARIOS = {
    "demo-ownership": {
        "question_id": "demo-ownership",
        "question": DEMO_INSTANCE["question"],
        "subject": "Ontology Migration",
        "abstain_subject": "Tom Chen",
        "abstain_object": "Ontology Migration",
        "label": "Ownership handoff (constructed)",
    },
    REAL_QUESTION_ID: {
        "question_id": REAL_QUESTION_ID,
        "question": REAL_QUESTION_TEXT,
        "subject": REAL_SUBJECT,
        "abstain_subject": REAL_SUBJECT,
        "abstain_object": "IKEA",
        "label": "LongMemEval " + REAL_QUESTION_ID + " (real benchmark data)",
    },
}

client: HydraDBClient | None = None


def get_client() -> HydraDBClient:
    global client
    if client is None:
        client = HydraDBClient()
    return client


def evidence_payload(rows, as_of: int) -> list[dict]:
    return [
        {
            "turn_id": e.turn_id,
            "content": e.content,
            "t_commit": e.t_commit,
            "leaked": e.t_commit > as_of,
        }
        for e in rows
    ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet the per-request stderr noise
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 -- http.server API
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self._html(ROOT / "demo" / "index.html")
            elif url.path == "/api/scenarios":
                self._json(list(SCENARIOS.values()))
            elif url.path == "/api/timeline":
                qid = params["question"]
                c = get_client()
                rows = []
                seen: set[int] = set()
                for key in SESSION_KEYS.get(qid, []):
                    pk = stable_id(key)
                    if pk in seen:  # haystacks can repeat a session id
                        continue
                    seen.add(pk)
                    hit = c.read(SESSION_BY_ID, {"sessionPk": pk})
                    rows.extend(hit)
                rows.sort(key=lambda r: r["t_commit"])
                self._json(rows)
            elif url.path == "/api/reconstruct":
                qid = params["question"]
                entity = params["entity"]
                as_of = int(params["asOf"])
                mode = params.get("mode", "fog")
                if mode == "fog":
                    rows = reconstruct_as_of(get_client(), entity, as_of, qid)
                else:
                    rows = naive_retrieve_all(get_client(), entity, qid)
                self._json(
                    {
                        "mode": mode,
                        "as_of": as_of,
                        "evidence": evidence_payload(rows, as_of),
                    }
                )
            elif url.path == "/api/abstain":
                qid = params["question"]
                as_of = int(params["asOf"])
                result = check_co_occurrence(
                    get_client(),
                    params["subject"],
                    params["object"],
                    as_of,
                    qid,
                )
                self._json(
                    {
                        "should_abstain": result.should_abstain,
                        "evidence_content": result.evidence_content,
                        "evidence_turn_id": result.evidence_turn_id,
                    }
                )
            else:
                self._json({"error": "not found"}, status=404)
        except Exception as exc:  # noqa: BLE001 -- surface, don't crash the demo
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)


def main() -> None:
    print("connecting to HydraDB...", flush=True)
    c = get_client()
    c.verify()
    print("ingesting demo scenario (idempotent)...", flush=True)
    ingest_instances(c, [DEMO_INSTANCE])
    print(f"serving on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
