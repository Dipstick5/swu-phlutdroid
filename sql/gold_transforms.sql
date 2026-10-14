-- Gold layer: weekly snapshots persisted for history.
-- Bronze -> Silver must run first.
-- Safe to re-run: uses INSERT OR IGNORE to avoid duplicates.

CREATE SCHEMA IF NOT EXISTS gold;

-- ---------------------------------------------------------------------------
-- 1. meta_snapshot: one row per (archetype, format, start_week, end_week, source)
--    We keep each ingest as a separate snapshot to preserve history.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.meta_snapshot (
    snapshot_id       INTEGER PRIMARY KEY,
    archetype_id      VARCHAR NOT NULL,
    leader_id         VARCHAR NOT NULL,
    base_id           VARCHAR NOT NULL,
    format            VARCHAR NOT NULL,
    start_week        INTEGER NOT NULL,
    end_week          INTEGER NOT NULL,
    source            VARCHAR NOT NULL DEFAULT 'swustats_online',
    num_plays         INTEGER,
    win_rate          DOUBLE,
    -- Wilson 95% CI
    wr_ci_low         DOUBLE,
    wr_ci_high        DOUBLE,
    avg_turns_win     DOUBLE,
    avg_turns_loss    DOUBLE,
    avg_cards_resourced_win  DOUBLE,
    avg_remaining_health_win DOUBLE,
    retrieved_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (archetype_id, format, start_week, end_week, source, retrieved_at)
);

CREATE SEQUENCE IF NOT EXISTS gold.meta_snapshot_seq START 1;

-- Merge latest silver rows into gold (idempotent on retrieved_at)
INSERT OR IGNORE INTO gold.meta_snapshot
SELECT
    nextval('gold.meta_snapshot_seq'),
    s.archetype_id,
    s.leader_id,
    s.base_id,
    s.format,
    s.start_week,
    s.end_week,
    'swustats_online'                  AS source,
    s.num_plays,
    s.win_rate / 100.0                 AS win_rate,
    -- Wilson score interval (95%, z=1.96)
    CASE WHEN s.num_plays > 0 THEN
        (s.win_rate/100.0 + 1.96*1.96/(2*s.num_plays)
         - 1.96*SQRT((s.win_rate/100.0*(1-s.win_rate/100.0)
                      + 1.96*1.96/(4*s.num_plays))/s.num_plays))
        / (1 + 1.96*1.96/s.num_plays)
    END                                AS wr_ci_low,
    CASE WHEN s.num_plays > 0 THEN
        (s.win_rate/100.0 + 1.96*1.96/(2*s.num_plays)
         + 1.96*SQRT((s.win_rate/100.0*(1-s.win_rate/100.0)
                      + 1.96*1.96/(4*s.num_plays))/s.num_plays))
        / (1 + 1.96*1.96/s.num_plays)
    END                                AS wr_ci_high,
    s.avg_turns_win,
    s.avg_turns_loss,
    s.avg_cards_resourced_win,
    s.avg_remaining_health_win,
    s.retrieved_at
FROM silver.meta_stat s
WHERE s.num_plays IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. matchup_snapshot: one row per (A, B, format, start_week, end_week, source)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.matchup_snapshot (
    snapshot_id       INTEGER PRIMARY KEY,
    archetype_a_id    VARCHAR NOT NULL,
    archetype_b_id    VARCHAR NOT NULL,
    format            VARCHAR NOT NULL,
    start_week        INTEGER NOT NULL,
    end_week          INTEGER NOT NULL,
    source            VARCHAR NOT NULL DEFAULT 'swustats_online',
    num_wins          INTEGER,
    num_plays         INTEGER,
    win_rate          DOUBLE,
    wr_ci_low         DOUBLE,
    wr_ci_high        DOUBLE,
    plays_going_first  INTEGER,
    wins_going_first   INTEGER,
    wins_going_second  INTEGER,
    retrieved_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (archetype_a_id, archetype_b_id, format, start_week, end_week, source, retrieved_at)
);

CREATE SEQUENCE IF NOT EXISTS gold.matchup_snapshot_seq START 1;

INSERT OR IGNORE INTO gold.matchup_snapshot
SELECT
    nextval('gold.matchup_snapshot_seq'),
    m.archetype_a_id,
    m.archetype_b_id,
    m.format,
    m.start_week,
    m.end_week,
    'swustats_online'                   AS source,
    m.num_wins,
    m.num_plays,
    CASE WHEN m.num_plays > 0
         THEN m.num_wins::DOUBLE / m.num_plays
    END                                 AS win_rate,
    CASE WHEN m.num_plays > 0 THEN
        (m.num_wins::DOUBLE/m.num_plays + 1.96*1.96/(2*m.num_plays)
         - 1.96*SQRT((m.num_wins::DOUBLE/m.num_plays
                      *(1-m.num_wins::DOUBLE/m.num_plays)
                      + 1.96*1.96/(4*m.num_plays))/m.num_plays))
        / (1 + 1.96*1.96/m.num_plays)
    END                                 AS wr_ci_low,
    CASE WHEN m.num_plays > 0 THEN
        (m.num_wins::DOUBLE/m.num_plays + 1.96*1.96/(2*m.num_plays)
         + 1.96*SQRT((m.num_wins::DOUBLE/m.num_plays
                      *(1-m.num_wins::DOUBLE/m.num_plays)
                      + 1.96*1.96/(4*m.num_plays))/m.num_plays))
        / (1 + 1.96*1.96/m.num_plays)
    END                                 AS wr_ci_high,
    m.plays_going_first,
    m.wins_going_first,
    m.wins_going_second,
    m.retrieved_at
FROM silver.matchup_stat m
WHERE m.num_plays IS NOT NULL;
