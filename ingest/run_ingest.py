#!/usr/bin/env python3
"""
Full ingest pipeline: bronze -> silver -> gold.
Designed to be called by GitHub Actions or manually.
"""

import argparse
import re
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
SQL_DIR = REPO_ROOT / "sql"


def _exec_sql_file(conn: duckdb.DuckDBPyConnection, path: Path) -> None:
    sql = path.read_text()
    sql_no_comments = re.sub(r"--[^\n]*", "", sql)
    for stmt in sql_no_comments.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)


def run_sql_file(conn: duckdb.DuckDBPyConnection, path: Path) -> None:
    sql = path.read_text()
    conn.executescript(sql)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", type=int, default=None,
                        help="Current week number (auto-calculated if omitted)")
    parser.add_argument("--start-week", type=int, default=None)
    parser.add_argument("--end-week", type=int, default=None)
    parser.add_argument("--format", default="premier", dest="fmt")
    parser.add_argument("--db", default="data/swu.duckdb")
    parser.add_argument("--min-plays", type=int, default=20)
    args = parser.parse_args()

    db_path = REPO_ROOT / args.db

    # Calculate current week if not provided
    if args.week is not None:
        start_week = end_week = args.week
    elif args.start_week is not None and args.end_week is not None:
        start_week = args.start_week
        end_week = args.end_week
    else:
        from datetime import date
        # Week 0 = Sep 20-26 2025; weeks run Sat-Fri
        epoch = date(2025, 9, 20)
        today = date.today()
        week = (today - epoch).days // 7
        start_week = end_week = week
        print(f"Auto-calculated week: {week}")

    # Step 1: bronze
    from ingest.bronze_ingest import init_schema, ingest_meta, ingest_matchups
    import sys
    sys.path.insert(0, str(REPO_ROOT))

    conn = duckdb.connect(str(db_path))
    init_schema(conn)
    print(f"Bronze: ingesting week {start_week}-{end_week}, {args.fmt}")
    archetypes = ingest_meta(conn, start_week, end_week, args.fmt)
    ingest_matchups(conn, archetypes, start_week, end_week, args.fmt,
                    min_plays=args.min_plays)
    conn.commit()

    # Step 2: silver
    print("Silver: running transforms...")
    _exec_sql_file(conn, SQL_DIR / "silver_transforms.sql")

    # Step 3: gold
    print("Gold: running transforms...")
    _exec_sql_file(conn, SQL_DIR / "gold_transforms.sql")

    conn.commit()
    conn.close()
    print("Ingest complete.")


if __name__ == "__main__":
    main()
