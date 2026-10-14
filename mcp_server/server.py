#!/usr/bin/env python3
"""
SWU Meta Agent MCP server (stdio transport).
Tools: resolve, meta, matchup, card, periods, tournaments, card_performance, search_docs.
"""

import math
import os
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.mcpserver import MCPServer

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "swu.duckdb"

BIAS_NOTE = (
    "Data from Karabast (online client). Only games where at least one player "
    "loaded their deck from SWUStats are tracked, and only Premier-format games "
    "that reached round 2+. Win rates reflect engaged players vs. the general "
    "queue. Compare archetypes to each other; do not treat absolute WR as truth."
)

SOURCE_INFO = {
    "id": "swustats_online",
    "name": "SWUStats (Karabast online games)",
    "bias_note": BIAS_NOTE,
}

app = MCPServer("swu-phlutdroid")


def get_db() -> duckdb.DuckDBPyConnection:
    db_path = os.environ.get("SWU_DB_PATH", str(DEFAULT_DB))
    return duckdb.connect(db_path, read_only=True)


def _matchup_by_aspect(
    conn: duckdb.DuckDBPyConnection,
    a_id: str,
    b_id: str | None,
    start_week: int,
    end_week: int,
    min_n: int,
    a_text: str,
    b_text: str | None,
) -> dict[str, Any]:
    """Query gold.matchup_by_aspect for leader+aspect group matchups."""
    if b_id:
        rows = conn.execute(
            """
            SELECT group_a_id, leader_a_id, leader_a_name, aspect_a,
                   group_b_id, leader_b_id, leader_b_name, aspect_b,
                   num_wins, num_plays, win_rate, wr_ci_low, wr_ci_high,
                   plays_going_first, wins_going_first, wins_going_second,
                   retrieved_at
            FROM gold.matchup_by_aspect
            WHERE group_a_id = ?
              AND group_b_id = ?
              AND format = 'premier'
              AND start_week >= ? AND end_week <= ?
              AND num_plays >= ?
              AND source = 'swustats_online'
            ORDER BY num_plays DESC
            """,
            [a_id, b_id, start_week, end_week, min_n],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT group_a_id, leader_a_id, leader_a_name, aspect_a,
                   group_b_id, leader_b_id, leader_b_name, aspect_b,
                   num_wins, num_plays, win_rate, wr_ci_low, wr_ci_high,
                   plays_going_first, wins_going_first, wins_going_second,
                   retrieved_at
            FROM gold.matchup_by_aspect
            WHERE group_a_id = ?
              AND format = 'premier'
              AND start_week >= ? AND end_week <= ?
              AND num_plays >= ?
              AND source = 'swustats_online'
            ORDER BY win_rate ASC
            """,
            [a_id, start_week, end_week, min_n],
        ).fetchall()

    cols = [
        "group_a_id", "leader_a_id", "leader_a_name", "aspect_a",
        "group_b_id", "leader_b_id", "leader_b_name", "aspect_b",
        "num_wins", "num_plays", "win_rate", "wr_ci_low", "wr_ci_high",
        "plays_going_first", "wins_going_first", "wins_going_second",
        "retrieved_at",
    ]
    matchups_out = []
    for r in rows:
        row = dict(zip(cols, r))
        for field in ("win_rate", "wr_ci_low", "wr_ci_high"):
            if row[field] is not None:
                row[field] = round(row[field], 4)
        matchups_out.append(row)

    return {
        "source": SOURCE_INFO,
        "window": {"start_week": start_week, "end_week": end_week},
        "group_by": "leader_aspect",
        "archetype_a": a_text,
        "archetype_b": b_text,
        "matchups": matchups_out,
    }


def current_week() -> int:
    """Week number relative to epoch Sep 20 2025."""
    epoch = date(2025, 9, 20)
    return (date.today() - epoch).days // 7


def _period_to_weeks(period: str) -> tuple[int, int]:
    if period == "current":
        w = current_week()
        return (w, w)
    if period.startswith("week:"):
        w = int(period.split(":")[1])
        return (w, w)
    if period.startswith("range:"):
        parts = period.split(":")[1].split("-")
        return (int(parts[0]), int(parts[1]))
    try:
        w = int(period)
        return (w, w)
    except ValueError:
        w = current_week()
        return (w, w)


def _resolve_id_from_db(conn: duckdb.DuckDBPyConnection, text: str, kind: str | None = None) -> list[dict]:
    """Resolve text to candidate IDs using alias table + fuzzy name match."""
    query = """
        SELECT target_id AS id, alias_text AS name, '' AS subtitle,
               kind, confidence
        FROM silver.alias
        WHERE lower(alias_text) = lower(?)
    """
    params: list[Any] = [text]
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY confidence DESC"

    rows = conn.execute(query, params).fetchall()
    cols = ["id", "name", "subtitle", "kind", "confidence"]
    results = [dict(zip(cols, r)) for r in rows]

    if results:
        return results

    fuzzy = f"%{text.lower()}%"
    arch_q = """
        SELECT archetype_id AS id,
               leader_name || ' / ' || base_name AS name,
               leader_subtitle AS subtitle,
               'archetype' AS kind,
               0.7 AS confidence
        FROM silver.archetype
        WHERE lower(leader_name) LIKE ?
           OR lower(base_name) LIKE ?
           OR lower(leader_subtitle) LIKE ?
    """
    if kind is None or kind == "archetype":
        rows = conn.execute(arch_q, [fuzzy, fuzzy, fuzzy]).fetchall()
        results.extend(dict(zip(cols, r)) for r in rows)

    return results[:10]


# ---------------------------------------------------------------------------
# Tool: resolve
# ---------------------------------------------------------------------------
@app.tool()
def resolve(
    text: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """
    Resolve natural-language text to card/archetype IDs.
    Returns [{id, name, subtitle, kind, confidence}].
    kind: 'archetype' | 'leader' | 'base' (optional filter)
    """
    conn = get_db()
    try:
        return _resolve_id_from_db(conn, text, kind)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool: meta
# ---------------------------------------------------------------------------
@app.tool()
def meta(
    format: str = "premier",
    period: str = "current",
    min_n: int = 50,
    sort: str = "plays",
    group_by_aspect: bool = False,
) -> dict[str, Any]:
    """
    Return archetype-level meta stats.
    period: 'current' | 'week:<N>' | 'range:<start>-<end>' | integer week number
    sort: 'plays' | 'wr'
    group_by_aspect: if True, aggregate by (leader + base_aspect) instead of specific archetype.
      'Ahsoka yellow' pools all Ahsoka/cunning-base variants together for wider sample sizes.
    Always includes source info and bias note.
    """
    start_week, end_week = _period_to_weeks(period)
    conn = get_db()
    try:
        if group_by_aspect:
            total_plays = conn.execute(
                """
                SELECT COALESCE(SUM(num_plays), 0)
                FROM gold.meta_by_aspect
                WHERE format = ? AND start_week >= ? AND end_week <= ?
                  AND source = 'swustats_online'
                """,
                [format, start_week, end_week],
            ).fetchone()[0]

            order = "num_plays DESC" if sort == "plays" else "win_rate DESC"
            rows = conn.execute(
                f"""
                SELECT leader_id, leader_name, leader_subtitle, base_aspect,
                       num_plays, win_rate, wr_ci_low, wr_ci_high,
                       base_variant_count, retrieved_at
                FROM gold.meta_by_aspect
                WHERE format = ? AND start_week >= ? AND end_week <= ?
                  AND num_plays >= ?
                  AND source = 'swustats_online'
                ORDER BY {order}
                """,
                [format, start_week, end_week, min_n],
            ).fetchall()

            cols = [
                "leader_id", "leader_name", "leader_subtitle", "base_aspect",
                "num_plays", "win_rate", "wr_ci_low", "wr_ci_high",
                "base_variant_count", "retrieved_at",
            ]
            groups = []
            for r in rows:
                row = dict(zip(cols, r))
                row["group_id"] = f"{row['leader_id']}|{row['base_aspect']}"
                row["share"] = round(row["num_plays"] / total_plays, 4) if total_plays else None
                for field in ("win_rate", "wr_ci_low", "wr_ci_high"):
                    if row[field] is not None:
                        row[field] = round(row[field], 4)
                groups.append(row)

            return {
                "source": SOURCE_INFO,
                "window": {"start_week": start_week, "end_week": end_week},
                "format": format,
                "group_by": "leader_aspect",
                "n": int(total_plays),
                "groups": groups,
            }

        total_plays = conn.execute(
            """
            SELECT COALESCE(SUM(num_plays), 0)
            FROM gold.meta_snapshot
            WHERE format = ? AND start_week >= ? AND end_week <= ?
              AND source = 'swustats_online'
            """,
            [format, start_week, end_week],
        ).fetchone()[0]

        order = "num_plays DESC" if sort == "plays" else "win_rate DESC"
        rows = conn.execute(
            f"""
            SELECT g.archetype_id, g.leader_id, g.base_id,
                   g.num_plays, g.win_rate, g.wr_ci_low, g.wr_ci_high,
                   g.avg_turns_win, g.retrieved_at,
                   a.leader_name, a.leader_subtitle, a.base_name, a.base_aspect
            FROM gold.meta_snapshot g
            LEFT JOIN silver.archetype a USING (archetype_id)
            WHERE g.format = ? AND g.start_week >= ? AND g.end_week <= ?
              AND g.num_plays >= ?
              AND g.source = 'swustats_online'
            ORDER BY {order}
            """,
            [format, start_week, end_week, min_n],
        ).fetchall()

        cols = [
            "archetype_id", "leader_id", "base_id",
            "num_plays", "win_rate", "wr_ci_low", "wr_ci_high",
            "avg_turns_win", "retrieved_at",
            "leader_name", "leader_subtitle", "base_name", "base_aspect",
        ]
        archetypes = []
        for r in rows:
            row = dict(zip(cols, r))
            row["share"] = round(row["num_plays"] / total_plays, 4) if total_plays else None
            for field in ("win_rate", "wr_ci_low", "wr_ci_high"):
                if row[field] is not None:
                    row[field] = round(row[field], 4)
            archetypes.append(row)

        return {
            "source": SOURCE_INFO,
            "window": {"start_week": start_week, "end_week": end_week},
            "format": format,
            "n": int(total_plays),
            "archetypes": archetypes,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool: matchup
# ---------------------------------------------------------------------------
@app.tool()
def matchup(
    archetype_a: str,
    archetype_b: str | None = None,
    period: str = "current",
    min_n: int = 8,
    group_by_aspect: bool = False,
) -> dict[str, Any]:
    """
    Return matchup data for archetype_a (optionally vs archetype_b).
    With no B, returns A's full spread sorted by win rate ascending (worst first).
    archetype_a / archetype_b can be archetype_id, alias text, or aspect alias
      (e.g. 'Ahsoka yellow', 'Krennic blue').
    group_by_aspect: if True, aggregate matchups by leader+aspect groups instead of
      specific archetypes. Automatically set when an aspect alias is resolved.
    """
    start_week, end_week = _period_to_weeks(period)
    conn = get_db()
    try:
        def _resolve(text: str, prefer_kind: str = "archetype") -> tuple[str | None, bool]:
            """Returns (id_or_pattern, is_aspect_group)."""
            # Direct archetype id
            if "/" in text and len(text.split("/")) == 2:
                return text, False
            # Direct aspect group id (leader|aspect)
            if "|" in text and len(text.split("|")) == 2:
                return text, True
            # Alias lookup
            results = _resolve_id_from_db(conn, text, "leader_aspect")
            if results:
                return results[0]["id"], True
            results = _resolve_id_from_db(conn, text, prefer_kind)
            if results:
                return results[0]["id"], False
            results = _resolve_id_from_db(conn, text, "leader")
            if results:
                return results[0]["id"] + "/%", False
            return None, False

        a_id, a_is_aspect = _resolve(archetype_a)
        if not a_id:
            return {"error": f"Could not resolve: {archetype_a}"}

        b_id, b_is_aspect = _resolve(archetype_b) if archetype_b else (None, False)

        use_aspect = group_by_aspect or a_is_aspect or b_is_aspect

        if use_aspect:
            return _matchup_by_aspect(conn, a_id, b_id, start_week, end_week, min_n, archetype_a, archetype_b)

        a_pat = a_id
        b_pat = b_id

        if b_pat:
            q = """
                SELECT g.archetype_a_id, g.archetype_b_id,
                       g.num_wins, g.num_plays,
                       g.win_rate, g.wr_ci_low, g.wr_ci_high,
                       g.plays_going_first, g.wins_going_first, g.wins_going_second,
                       g.retrieved_at,
                       a.leader_name || ' / ' || a.base_name AS opponent_name
                FROM gold.matchup_snapshot g
                LEFT JOIN silver.archetype a ON a.archetype_id = g.archetype_b_id
                WHERE g.archetype_a_id LIKE ?
                  AND g.archetype_b_id LIKE ?
                  AND g.format = 'premier'
                  AND g.start_week >= ? AND g.end_week <= ?
                  AND g.num_plays >= ?
                  AND g.source = 'swustats_online'
                ORDER BY g.num_plays DESC
            """
            params = [a_pat, b_pat, start_week, end_week, min_n]
        else:
            q = """
                SELECT g.archetype_a_id, g.archetype_b_id,
                       g.num_wins, g.num_plays,
                       g.win_rate, g.wr_ci_low, g.wr_ci_high,
                       g.plays_going_first, g.wins_going_first, g.wins_going_second,
                       g.retrieved_at,
                       a.leader_name || ' / ' || a.base_name AS opponent_name
                FROM gold.matchup_snapshot g
                LEFT JOIN silver.archetype a ON a.archetype_id = g.archetype_b_id
                WHERE g.archetype_a_id LIKE ?
                  AND g.format = 'premier'
                  AND g.start_week >= ? AND g.end_week <= ?
                  AND g.num_plays >= ?
                  AND g.source = 'swustats_online'
                ORDER BY g.win_rate ASC
            """
            params = [a_pat, start_week, end_week, min_n]

        rows = conn.execute(q, params).fetchall()
        cols = [
            "archetype_a_id", "archetype_b_id",
            "num_wins", "num_plays", "win_rate", "wr_ci_low", "wr_ci_high",
            "plays_going_first", "wins_going_first", "wins_going_second",
            "retrieved_at", "opponent_name",
        ]

        matchups_out = []
        for r in rows:
            row = dict(zip(cols, r))
            for field in ("win_rate", "wr_ci_low", "wr_ci_high"):
                if row[field] is not None:
                    row[field] = round(row[field], 4)
            if row["plays_going_first"] and row["num_plays"]:
                row["first_player_split"] = round(row["plays_going_first"] / row["num_plays"], 3)
            matchups_out.append(row)

        reverse: list[dict] = []
        if b_pat and "%" not in (b_pat or ""):
            rev_rows = conn.execute(
                """
                SELECT g.archetype_a_id, g.archetype_b_id,
                       g.num_wins, g.num_plays,
                       g.win_rate, g.wr_ci_low, g.wr_ci_high,
                       g.plays_going_first, g.wins_going_first, g.wins_going_second,
                       g.retrieved_at,
                       a.leader_name || ' / ' || a.base_name AS opponent_name
                FROM gold.matchup_snapshot g
                LEFT JOIN silver.archetype a ON a.archetype_id = g.archetype_b_id
                WHERE g.archetype_a_id LIKE ?
                  AND g.archetype_b_id LIKE ?
                  AND g.format = 'premier'
                  AND g.start_week >= ? AND g.end_week <= ?
                  AND g.num_plays >= ?
                  AND g.source = 'swustats_online'
                ORDER BY g.num_plays DESC
                """,
                [b_pat, a_pat, start_week, end_week, min_n],
            ).fetchall()
            for r in rev_rows:
                row = dict(zip(cols, r))
                for field in ("win_rate", "wr_ci_low", "wr_ci_high"):
                    if row[field] is not None:
                        row[field] = round(row[field], 4)
                reverse.append(row)

        return {
            "source": SOURCE_INFO,
            "window": {"start_week": start_week, "end_week": end_week},
            "archetype_a": archetype_a,
            "archetype_b": archetype_b,
            "matchups": matchups_out,
            "reverse_perspective": reverse,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool: periods
# ---------------------------------------------------------------------------
@app.tool()
def periods(format: str = "premier") -> list[dict[str, Any]]:
    """Return available data periods (start/end weeks and dates) for a format."""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT start_week, end_week, COUNT(*) AS archetype_count
            FROM gold.meta_snapshot
            WHERE format = ? AND source = 'swustats_online'
            GROUP BY start_week, end_week
            ORDER BY start_week DESC
            """,
            [format],
        ).fetchall()
        from datetime import timedelta
        epoch = date(2025, 9, 20)
        result = []
        for start_week, end_week, count in rows:
            result.append({
                "period_id": f"range:{start_week}-{end_week}",
                "start_week": start_week,
                "end_week": end_week,
                "start_date": (epoch + timedelta(weeks=start_week)).isoformat(),
                "end_date": (epoch + timedelta(weeks=end_week + 1) - timedelta(days=1)).isoformat(),
                "archetype_count": count,
                "trigger": "weekly",
            })
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool: card
# ---------------------------------------------------------------------------
@app.tool()
def card(id: str) -> dict[str, Any]:
    """
    Return card facts (name, type, aspects, text, legality).
    Leaders and bases are available from the archetype dimension.
    Full card database requires a separate card ingest.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT DISTINCT leader_id, leader_name, leader_subtitle FROM silver.archetype WHERE leader_id = ? LIMIT 1",
            [id],
        ).fetchone()
        if row:
            return {"id": row[0], "name": row[1], "subtitle": row[2], "type": "leader",
                    "note": "Full card text/rules require card ingest."}

        row = conn.execute(
            "SELECT DISTINCT base_id, base_name, base_subtitle FROM silver.archetype WHERE base_id = ? LIMIT 1",
            [id],
        ).fetchone()
        if row:
            return {"id": row[0], "name": row[1], "subtitle": row[2], "type": "base",
                    "note": "Full card text/rules require card ingest."}

        return {"error": f"Card {id} not found. Run card ingest for full database."}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool: search_docs (stub)
# ---------------------------------------------------------------------------
@app.tool()
def search_docs(query: str, entity_ids: list[str] | None = None) -> dict[str, Any]:
    """
    Vector search over rules, rulings, ban announcements, set articles.
    Not yet implemented.
    """
    return {"results": [], "note": "Document search not yet implemented."}


# ---------------------------------------------------------------------------
# Tool: tournaments (stub)
# ---------------------------------------------------------------------------
@app.tool()
def tournaments(
    format: str = "premier",
    period: str = "current",
    tier: str | None = None,
) -> dict[str, Any]:
    """Paper tournament results (Melee/SWUBase). Not yet ingested."""
    return {
        "results": [],
        "note": "Paper tournament data not yet ingested.",
        "warning": "Never blend paper and online data.",
    }


# ---------------------------------------------------------------------------
# Tool: card_performance (stub)
# ---------------------------------------------------------------------------
@app.tool()
def card_performance(
    card_id: str | None = None,
    archetype_id: str | None = None,
    period: str = "current",
) -> dict[str, Any]:
    """Per-card performance within an archetype. Requires per-card SWUStats data."""
    return {
        "results": [],
        "note": "Per-card data pending confirmation of CardMetaStats.php availability.",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    asyncio.run(app.run_stdio_async())
