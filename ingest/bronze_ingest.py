#!/usr/bin/env python3
"""
Bronze layer ingest: pulls SWUStats meta + matchup data for a given week range
and stores raw JSON in DuckDB bronze schema.

Usage:
    python bronze_ingest.py [--week 55] [--start-week 55] [--end-week 55]
                            [--format premier] [--db data/swu.duckdb]
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

SWUSTATS_BASE = "https://swustats.net/TCGEngine"
META_API = f"{SWUSTATS_BASE}/Stats/DeckMetaStatsAPI.php"
MATCHUP_API = f"{SWUSTATS_BASE}/APIs/DeckMetaMatchupStatsAPI.php"

# Be polite: sleep between requests
REQUEST_DELAY_S = 1.0


def _get(url: str, params: dict) -> list | dict:
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE SCHEMA IF NOT EXISTS bronze;

        CREATE TABLE IF NOT EXISTS bronze.meta_raw (
            id             INTEGER PRIMARY KEY,
            start_week     INTEGER NOT NULL,
            end_week       INTEGER NOT NULL,
            consolidate    INTEGER NOT NULL,
            format         VARCHAR NOT NULL,
            payload        JSON NOT NULL,
            retrieved_at   TIMESTAMPTZ NOT NULL
        );

        CREATE SEQUENCE IF NOT EXISTS bronze.meta_raw_seq START 1;

        CREATE TABLE IF NOT EXISTS bronze.matchup_raw (
            id             INTEGER PRIMARY KEY,
            leader_id      VARCHAR NOT NULL,
            base_id        VARCHAR NOT NULL,
            start_week     INTEGER NOT NULL,
            end_week       INTEGER NOT NULL,
            consolidate    INTEGER NOT NULL,
            format         VARCHAR NOT NULL,
            payload        JSON NOT NULL,
            retrieved_at   TIMESTAMPTZ NOT NULL
        );

        CREATE SEQUENCE IF NOT EXISTS bronze.matchup_raw_seq START 1;
    """)


def ingest_meta(
    conn: duckdb.DuckDBPyConnection,
    start_week: int,
    end_week: int,
    fmt: str,
    consolidate: int = 1,
) -> list[dict]:
    """Fetch deck-level meta table and store in bronze.meta_raw."""
    params = {
        "startWeek": start_week,
        "endWeek": end_week,
        "consolidate": consolidate,
        "format": fmt,
    }
    data = _get(META_API, params)
    retrieved_at = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO bronze.meta_raw
            (id, start_week, end_week, consolidate, format, payload, retrieved_at)
        VALUES (nextval('bronze.meta_raw_seq'), ?, ?, ?, ?, ?, ?)
        """,
        [start_week, end_week, consolidate, fmt, json.dumps(data), retrieved_at],
    )
    print(f"  meta: {len(data)} archetypes (week {start_week}-{end_week}, {fmt})")
    return data


def ingest_matchups(
    conn: duckdb.DuckDBPyConnection,
    archetypes: list[dict],
    start_week: int,
    end_week: int,
    fmt: str,
    consolidate: int = 1,
    min_plays: int = 20,
) -> None:
    """Fetch matchup data for each archetype that meets min_plays threshold."""
    retrieved_at = datetime.now(timezone.utc).isoformat()
    fetched = 0
    skipped = 0

    for arch in archetypes:
        if arch["numPlays"] < min_plays:
            skipped += 1
            continue

        leader_id = arch["leaderID"]
        base_id = arch["baseID"]
        params = {
            "leaderID": leader_id,
            "baseID": base_id,
            "startWeek": start_week,
            "endWeek": end_week,
            "consolidate": consolidate,
            "format": fmt,
        }
        try:
            data = _get(MATCHUP_API, params)
        except Exception as e:
            print(f"  matchup SKIP {leader_id}/{base_id}: {e}")
            continue

        conn.execute(
            """
            INSERT INTO bronze.matchup_raw
                (id, leader_id, base_id, start_week, end_week,
                 consolidate, format, payload, retrieved_at)
            VALUES (nextval('bronze.matchup_raw_seq'), ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                leader_id, base_id, start_week, end_week,
                consolidate, fmt, json.dumps(data), retrieved_at,
            ],
        )
        fetched += 1
        time.sleep(REQUEST_DELAY_S)

    print(f"  matchups: {fetched} fetched, {skipped} skipped (< {min_plays} plays)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", type=int, help="Shortcut: set both start and end week")
    parser.add_argument("--start-week", type=int, default=55)
    parser.add_argument("--end-week", type=int, default=55)
    parser.add_argument("--format", default="premier", dest="fmt")
    parser.add_argument("--db", default="data/swu.duckdb")
    parser.add_argument("--min-plays", type=int, default=20,
                        help="Min plays to fetch matchups for an archetype")
    args = parser.parse_args()

    if args.week is not None:
        args.start_week = args.end_week = args.week

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path))
    init_schema(conn)

    print(f"Ingesting week {args.start_week}-{args.end_week}, format={args.fmt}")
    archetypes = ingest_meta(conn, args.start_week, args.end_week, args.fmt)
    ingest_matchups(conn, archetypes, args.start_week, args.end_week, args.fmt,
                    min_plays=args.min_plays)

    conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
