#!/usr/bin/env python3
"""
Full pipeline update: bronze ingest → silver transforms → gold transforms.

Usage:
    python update.py                  # current week, premier format
    python update.py --week 54        # specific week
    python update.py --format open    # different format
    python update.py --db path/to.db  # different db path
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

import duckdb

from ingest.bronze_ingest import init_schema, ingest_meta, ingest_matchups

EPOCH = date(2025, 9, 20)
REPO_ROOT = Path(__file__).parent


def current_week() -> int:
    return (date.today() - EPOCH).days // 7


def run_sql_file(conn: duckdb.DuckDBPyConnection, path: Path) -> None:
    conn.execute(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description="Update swu-phlutdroid data pipeline")
    parser.add_argument("--week", type=int, help="Week number (default: current)")
    parser.add_argument("--start-week", type=int, help="Start of week range")
    parser.add_argument("--end-week", type=int, help="End of week range")
    parser.add_argument("--format", default="premier", dest="fmt")
    parser.add_argument("--db", default="data/swu.duckdb")
    parser.add_argument("--min-plays", type=int, default=20)
    args = parser.parse_args()

    if args.week is not None:
        start_week = end_week = args.week
    elif args.start_week is not None:
        start_week = args.start_week
        end_week = args.end_week or args.start_week
    else:
        start_week = end_week = current_week()

    week_date = (EPOCH + timedelta(weeks=start_week)).strftime("%b %d, %Y")
    db_path = REPO_ROOT / args.db

    print(f"swu-phlutdroid update")
    print(f"  week    : {start_week}" + (f"–{end_week}" if end_week != start_week else f"  ({week_date})"))
    print(f"  format  : {args.fmt}")
    print(f"  db      : {db_path}")
    print()

    conn = duckdb.connect(str(db_path))

    # ── Bronze ────────────────────────────────────────────────────────────────
    print("[ 1/3 ] Bronze ingest...")
    init_schema(conn)
    archetypes = ingest_meta(conn, start_week, end_week, args.fmt)
    ingest_matchups(conn, archetypes, start_week, end_week, args.fmt,
                    min_plays=args.min_plays)
    conn.commit()

    # ── Silver ────────────────────────────────────────────────────────────────
    print("\n[ 2/3 ] Silver transforms...")
    run_sql_file(conn, REPO_ROOT / "sql" / "silver_transforms.sql")
    n_arch = conn.execute("SELECT COUNT(*) FROM silver.archetype").fetchone()[0]
    n_alias = conn.execute("SELECT COUNT(*) FROM silver.alias").fetchone()[0]
    print(f"  archetypes : {n_arch}")
    print(f"  aliases    : {n_alias}")

    # ── Gold ──────────────────────────────────────────────────────────────────
    print("\n[ 3/3 ] Gold transforms...")
    run_sql_file(conn, REPO_ROOT / "sql" / "gold_transforms.sql")

    # Summary from deduped view
    rows = conn.execute("""
        WITH deduped AS (
            SELECT *
            FROM gold.meta_snapshot
            WHERE format = ? AND start_week = ? AND end_week = ?
              AND source = 'swustats_online'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY archetype_id, format, start_week, end_week
                ORDER BY retrieved_at DESC
            ) = 1
        )
        SELECT COUNT(*), SUM(num_plays) FROM deduped
    """, [args.fmt, start_week, end_week]).fetchone()
    n_archetypes, total_plays = rows

    top = conn.execute("""
        WITH deduped AS (
            SELECT *
            FROM gold.meta_snapshot
            WHERE format = ? AND start_week = ? AND end_week = ?
              AND source = 'swustats_online'
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY archetype_id, format, start_week, end_week
                ORDER BY retrieved_at DESC
            ) = 1
        )
        SELECT a.leader_name, a.base_name,
               d.num_plays, ROUND(d.win_rate * 100, 1) AS wr
        FROM deduped d
        JOIN silver.archetype a USING (archetype_id)
        WHERE d.num_plays >= 20
        ORDER BY d.win_rate DESC
        LIMIT 5
    """, [args.fmt, start_week, end_week]).fetchall()

    print(f"  archetypes : {n_archetypes}")
    print(f"  total plays: {total_plays:,}")
    print(f"  top 5 by WR:")
    for r in top:
        print(f"    {r[0]:20s} / {r[1]:25s}  {r[2]:4} plays  {r[3]}% WR")

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
