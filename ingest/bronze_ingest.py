#!/usr/bin/env python3
"""
Bronze layer ingest: pulls SWUStats data and stores raw JSON in DuckDB.

Sources:
  - DeckMetaStatsAPI.php       → bronze.meta_raw
  - MetaMatchupStatsAPI.php    → bronze.matchup_raw  (bulk, one call)
  - CardMetaStatsAPI.php       → bronze.card_stat_raw (per-archetype)
  - GetMeleeTournaments.php    → bronze.tournament_raw
  - GetMeleeTournament.php     → bronze.tournament_deck_raw (per-tournament)

Usage:
    python -m ingest.bronze_ingest --week 55 --format premier --db data/swu.duckdb
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

SWUSTATS_BASE = "https://swustats.net/TCGEngine"
META_API          = f"{SWUSTATS_BASE}/Stats/DeckMetaStatsAPI.php"
BULK_MATCHUP_API  = f"{SWUSTATS_BASE}/APIs/MetaMatchupStatsAPI.php"
CARD_STAT_API     = f"{SWUSTATS_BASE}/Stats/CardMetaStatsAPI.php"
MELEE_LIST_API    = f"{SWUSTATS_BASE}/APIs/GetMeleeTournaments.php"
MELEE_DETAIL_API  = f"{SWUSTATS_BASE}/APIs/GetMeleeTournament.php"

REQUEST_DELAY_S = 0.5


def _get(url: str, params: dict) -> list | dict:
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Schema ──────────────────────────────────────────────────────────────────

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

        -- Bulk matchup grid (one row per ingest run; leaderID/baseID inside JSON items)
        -- Schema changed from per-archetype rows to bulk: drop/recreate to avoid column mismatch.
        DROP TABLE IF EXISTS bronze.matchup_raw;
        DROP SEQUENCE IF EXISTS bronze.matchup_raw_seq;
        CREATE TABLE bronze.matchup_raw (
            id             INTEGER PRIMARY KEY,
            start_week     INTEGER NOT NULL,
            end_week       INTEGER NOT NULL,
            format         VARCHAR NOT NULL,
            payload        JSON NOT NULL,
            retrieved_at   TIMESTAMPTZ NOT NULL
        );
        CREATE SEQUENCE IF NOT EXISTS bronze.matchup_raw_seq START 1;

        -- Per-archetype card stats
        CREATE TABLE IF NOT EXISTS bronze.card_stat_raw (
            id             INTEGER PRIMARY KEY,
            leader_id      VARCHAR NOT NULL,
            base_id        VARCHAR NOT NULL,
            start_week     INTEGER NOT NULL,
            end_week       INTEGER NOT NULL,
            format         VARCHAR NOT NULL,
            payload        JSON NOT NULL,
            retrieved_at   TIMESTAMPTZ NOT NULL
        );
        CREATE SEQUENCE IF NOT EXISTS bronze.card_stat_raw_seq START 1;

        -- Melee tournament list snapshot
        CREATE TABLE IF NOT EXISTS bronze.tournament_raw (
            id             INTEGER PRIMARY KEY,
            payload        JSON NOT NULL,
            retrieved_at   TIMESTAMPTZ NOT NULL
        );
        CREATE SEQUENCE IF NOT EXISTS bronze.tournament_raw_seq START 1;

        -- Per-tournament detail (decks + standings)
        CREATE TABLE IF NOT EXISTS bronze.tournament_deck_raw (
            id             INTEGER PRIMARY KEY,
            tournament_id  INTEGER NOT NULL,
            payload        JSON NOT NULL,
            retrieved_at   TIMESTAMPTZ NOT NULL
        );
        CREATE SEQUENCE IF NOT EXISTS bronze.tournament_deck_raw_seq START 1;
    """)


# ── Meta ─────────────────────────────────────────────────────────────────────

def ingest_meta(
    conn: duckdb.DuckDBPyConnection,
    start_week: int,
    end_week: int,
    fmt: str,
    consolidate: int = 1,
) -> list[dict]:
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


# ── Bulk matchup ─────────────────────────────────────────────────────────────

def ingest_matchups_bulk(
    conn: duckdb.DuckDBPyConnection,
    start_week: int,
    end_week: int,
    fmt: str,
) -> None:
    """One call fetches the entire matchup grid — replaces per-archetype loop."""
    params = {
        "startWeek": start_week,
        "endWeek": end_week,
        "format": fmt,
    }
    data = _get(BULK_MATCHUP_API, params)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO bronze.matchup_raw
            (id, start_week, end_week, format, payload, retrieved_at)
        VALUES (nextval('bronze.matchup_raw_seq'), ?, ?, ?, ?, ?)
        """,
        [start_week, end_week, fmt, json.dumps(data), retrieved_at],
    )
    print(f"  matchups: {len(data)} pairs (bulk, 1 request)")


# ── Card stats ────────────────────────────────────────────────────────────────

def ingest_card_stats(
    conn: duckdb.DuckDBPyConnection,
    archetypes: list[dict],
    start_week: int,
    end_week: int,
    fmt: str,
    min_plays: int = 20,
) -> None:
    """Per-archetype card play/resource/win rates."""
    retrieved_at = datetime.now(timezone.utc).isoformat()
    fetched = skipped = 0
    for arch in archetypes:
        if arch["numPlays"] < min_plays:
            skipped += 1
            continue
        leader_id = arch["leaderID"]
        base_id   = arch["baseID"]
        params = {
            "cardId":     f"{leader_id}/{base_id}",
            "startWeek":  start_week,
            "endWeek":    end_week,
            "format":     fmt,
        }
        try:
            data = _get(CARD_STAT_API, params)
        except Exception as e:
            print(f"  card_stat SKIP {leader_id}/{base_id}: {e}")
            continue
        if not data:
            skipped += 1
            continue
        conn.execute(
            """
            INSERT INTO bronze.card_stat_raw
                (id, leader_id, base_id, start_week, end_week, format, payload, retrieved_at)
            VALUES (nextval('bronze.card_stat_raw_seq'), ?, ?, ?, ?, ?, ?, ?)
            """,
            [leader_id, base_id, start_week, end_week, fmt,
             json.dumps(data), retrieved_at],
        )
        fetched += 1
        time.sleep(REQUEST_DELAY_S)
    print(f"  card stats: {fetched} fetched, {skipped} skipped")


# ── Melee tournaments ─────────────────────────────────────────────────────────

def ingest_melee_tournaments(
    conn: duckdb.DuckDBPyConnection,
    days_back: int = 90,
    max_tournaments: int = 20,
) -> list[int]:
    """Fetch recent Melee tournament list and per-tournament details."""
    from datetime import date, timedelta
    date_from = (date.today() - timedelta(days=days_back)).isoformat()

    params = {
        "limit":     max_tournaments,
        "date_from": date_from,
        "sort":      "tournamentDate DESC",
    }
    list_data = _get(MELEE_LIST_API, params)
    retrieved_at = datetime.now(timezone.utc).isoformat()

    tournaments = list_data.get("tournaments", []) if isinstance(list_data, dict) else []
    conn.execute(
        """
        INSERT INTO bronze.tournament_raw
            (id, payload, retrieved_at)
        VALUES (nextval('bronze.tournament_raw_seq'), ?, ?)
        """,
        [json.dumps(list_data), retrieved_at],
    )
    print(f"  melee list: {len(tournaments)} tournaments (last {days_back} days)")

    # Fetch details for each tournament
    fetched = 0
    existing_ids = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT tournament_id FROM bronze.tournament_deck_raw"
        ).fetchall()
    }
    for t in tournaments:
        tid = t.get("id") or t.get("link")
        if not tid or tid in existing_ids:
            continue
        try:
            detail = _get(MELEE_DETAIL_API, {"id": tid, "include_matchups": 1})
        except Exception as e:
            print(f"  melee detail SKIP {tid}: {e}")
            continue
        conn.execute(
            """
            INSERT INTO bronze.tournament_deck_raw
                (id, tournament_id, payload, retrieved_at)
            VALUES (nextval('bronze.tournament_deck_raw_seq'), ?, ?, ?)
            """,
            [tid, json.dumps(detail), retrieved_at],
        )
        fetched += 1
        time.sleep(REQUEST_DELAY_S)

    print(f"  melee detail: {fetched} new tournaments ingested")
    return [t.get("id") for t in tournaments if t.get("id")]


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", type=int)
    parser.add_argument("--start-week", type=int, default=55)
    parser.add_argument("--end-week",   type=int, default=55)
    parser.add_argument("--format",     default="premier", dest="fmt")
    parser.add_argument("--db",         default="data/swu.duckdb")
    parser.add_argument("--min-plays",  type=int, default=20)
    parser.add_argument("--no-cards",   action="store_true", help="Skip card stats")
    parser.add_argument("--no-melee",   action="store_true", help="Skip Melee ingest")
    parser.add_argument("--melee-days", type=int, default=90)
    args = parser.parse_args()

    if args.week is not None:
        args.start_week = args.end_week = args.week

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    init_schema(conn)

    print(f"Ingesting week {args.start_week}-{args.end_week}, format={args.fmt}")
    archetypes = ingest_meta(conn, args.start_week, args.end_week, args.fmt)
    ingest_matchups_bulk(conn, args.start_week, args.end_week, args.fmt)

    if not args.no_cards:
        ingest_card_stats(conn, archetypes, args.start_week, args.end_week,
                          args.fmt, min_plays=args.min_plays)

    if not args.no_melee:
        ingest_melee_tournaments(conn, days_back=args.melee_days)

    conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
