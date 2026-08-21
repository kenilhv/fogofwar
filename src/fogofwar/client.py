"""Thin client wrapper around HydraDB's Bolt endpoint.

HydraDB speaks Neo4j-driver-compatible Bolt (5.1-5.4), so this is the real
`neo4j` Python driver, not a stub -- pointed at a local HydraDB node per its
own README quickstart (`docker run ... -p 7687:7687 ...` or the source-build
path). Batched writes (UNWIND ... MERGE ... SET) only work through the client
transport, not the in-process shard API, which is exactly what this wraps.

Read consistency: causal is the default hot path (cheap, current durable
view). strong pays the object-store freshness cost -- reserved here for the
one query that matters most: recomputing a result right before it's reported
as the final, honest answer (see query/pointintime.py).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

from neo4j import GraphDatabase, Driver, bearer_auth
from neo4j.exceptions import DatabaseError, ServiceUnavailable, SessionExpired


@dataclass(frozen=True)
class HydraDBConfig:
    uri: str = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    token: str = os.environ.get("HYDRADB_TOKEN", "local-development-token-32-bytes")
    database: str = os.environ.get("HYDRADB_NAMESPACE", "default")


class HydraDBClient:
    """Wraps a `neo4j.Driver` with the two write/read shapes this project uses."""

    def __init__(self, config: HydraDBConfig | None = None):
        self.config = config or HydraDBConfig()
        # HydraDB's documented auth is a bearer token (its HTTP examples use
        # `Authorization: Bearer $TOKEN`; the local dev token comes from
        # GRAPH_AUTH_TOKEN_FILE). Its exact Bolt-side auth handshake isn't
        # spelled out in the README beyond "Authentication ... part of the
        # server runtime" -- this is the standard Neo4j-driver bearer-auth
        # helper, the most likely match, but verify() below is exactly the
        # smoke test to confirm it against a real running node rather than
        # trusting this comment.
        self._driver: Driver = GraphDatabase.driver(
            self.config.uri, auth=bearer_auth(self.config.token)
        )

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "HydraDBClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def verify(self) -> None:
        """Round-trips a trivial write+read -- a listening port isn't proof.

        The probe is a relationship path, not a bare node: HydraDB's CREATE
        only accepts relationship paths (cypher-compat.md, confirmed live --
        a lone-node CREATE fails with "only one-hop edge patterns are
        executable in Query engine CREATE").

        The probe is MERGEd, never deleted. An earlier version cleaned up
        with DETACH DELETE, which turned out to scan the ENTIRE cell's
        relationship set regardless of the vertex's actual degree
        ("delete_vertex_scan_relationships rejected by admission control:
        actual 1000001 exceeds limit 1000000", observed live once the graph
        crossed ~1M edges) -- so verify() started failing on a populated
        database purely because of its own cleanup. Two permanent one-edge
        probe vertices are the cheaper price.
        """
        a, b = 999_999_998, 999_999_999
        with self._driver.session() as session:
            session.run(
                "MERGE (a:_FogOfWarProbe {id: $a})-[:_PROBE]->(b:_FogOfWarProbe {id: $b})",
                a=a,
                b=b,
            ).consume()
            # LIMIT 1: past CREATE-based probes left parallel _PROBE edges,
            # and HydraDB treats parallel relationships as distinct rows.
            result = session.run(
                "MATCH (a:_FogOfWarProbe {id: $a})-[:_PROBE]->(b) RETURN b.id AS id LIMIT 1",
                a=a,
            )
            row = result.single()
            if row is None or row["id"] != b:
                raise RuntimeError(
                    "HydraDB verify failed: probe write did not round-trip"
                )

    def write_batch(self, query: str, rows: list[dict], batch_size: int = 200) -> int:
        """Runs an UNWIND ... MERGE ... SET batch write in chunks.

        HydraDB requires the UNWIND input to come from a parameter, never an
        inline list -- `rows` is that parameter. Chunking keeps individual
        requests within reasonable size/latency budgets rather than sending
        tens of thousands of rows in one call.

        Note: install `neo4j-rust-ext` (it's in this project's deps). The
        driver's pure-Python packstream packer crashed twice under this
        workload's large chat-turn payloads (a segfault, then a corrupted-
        builtin TypeError deep in `_py_pack`); the Rust codec bypasses that
        path entirely and is auto-detected by the driver when present.
        """
        written = 0
        for start in range(0, len(rows), batch_size):
            chunk = rows[start : start + batch_size]
            self._run_write_with_retry(query, chunk)
            written += len(chunk)
        return written

    # Transient server-side failures observed live under sustained bulk
    # write: "internal query execution error" (server log: "corrupt value at
    # client/query/executor: query executor panicked" -- the node stays up)
    # and SessionExpired when a connection dies mid-request. Every batch
    # write here is UNWIND+MERGE, i.e. idempotent, so blind retry is safe by
    # construction. Each attempt uses a fresh session so a defunct pooled
    # connection can't poison the retry.
    _RETRY_DELAYS = (1.0, 3.0, 8.0, 15.0)

    def _run_write_with_retry(self, query: str, chunk: list[dict]) -> None:
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0,) + self._RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                with self._driver.session(database=self.config.database) as session:
                    session.run(query, rows=chunk).consume()
                return
            except (DatabaseError, SessionExpired, ServiceUnavailable) as exc:
                last_error = exc
                print(
                    f"  transient write failure (attempt {attempt + 1}): "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
        raise RuntimeError(
            f"write batch failed after {1 + len(self._RETRY_DELAYS)} attempts"
        ) from last_error

    def read(
        self, query: str, params: dict | None = None, consistency: str = "causal"
    ) -> list[dict]:
        """Runs a read query. consistency is carried in Bolt RUN metadata."""
        params = dict(params or {})
        with self._driver.session(database=self.config.database) as session:
            result = session.run(
                query, parameters=params, hydradb_consistency=consistency
            )
            return [dict(record) for record in result]
