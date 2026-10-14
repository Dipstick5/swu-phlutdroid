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

-- ---------------------------------------------------------------------------
-- 3. meta_by_aspect: aggregate meta stats by (leader_id, base_aspect)
--    Pools all specific-base variants of a leader into one aspect group.
--    Deduplicates to the latest retrieved_at per archetype before aggregating.
--    Wilson CI is recomputed from the pooled counts.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.meta_by_aspect AS
WITH deduped AS (
    SELECT *
    FROM gold.meta_snapshot
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY archetype_id, format, start_week, end_week, source
        ORDER BY retrieved_at DESC
    ) = 1
)
SELECT
    g.leader_id,
    a.leader_name,
    a.leader_subtitle,
    a.base_aspect,
    g.format,
    g.start_week,
    g.end_week,
    g.source,
    MAX(g.retrieved_at)                               AS retrieved_at,
    SUM(g.num_plays)                                  AS num_plays,
    -- Weighted average win rate (weighted by plays)
    SUM(g.win_rate * g.num_plays) / NULLIF(SUM(g.num_plays), 0) AS win_rate,
    -- Wilson CI from pooled counts
    CASE WHEN SUM(g.num_plays) > 0 THEN
        (SUM(g.win_rate * g.num_plays) / SUM(g.num_plays)
         + 1.96*1.96 / (2 * SUM(g.num_plays))
         - 1.96 * SQRT((SUM(g.win_rate * g.num_plays) / SUM(g.num_plays)
                        * (1 - SUM(g.win_rate * g.num_plays) / SUM(g.num_plays))
                        + 1.96*1.96 / (4 * SUM(g.num_plays))) / SUM(g.num_plays)))
        / (1 + 1.96*1.96 / SUM(g.num_plays))
    END                                               AS wr_ci_low,
    CASE WHEN SUM(g.num_plays) > 0 THEN
        (SUM(g.win_rate * g.num_plays) / SUM(g.num_plays)
         + 1.96*1.96 / (2 * SUM(g.num_plays))
         + 1.96 * SQRT((SUM(g.win_rate * g.num_plays) / SUM(g.num_plays)
                        * (1 - SUM(g.win_rate * g.num_plays) / SUM(g.num_plays))
                        + 1.96*1.96 / (4 * SUM(g.num_plays))) / SUM(g.num_plays)))
        / (1 + 1.96*1.96 / SUM(g.num_plays))
    END                                               AS wr_ci_high,
    COUNT(DISTINCT g.archetype_id)                    AS base_variant_count
FROM deduped g
JOIN silver.archetype a USING (archetype_id)
GROUP BY
    g.leader_id, a.leader_name, a.leader_subtitle, a.base_aspect,
    g.format, g.start_week, g.end_week, g.source;

-- ---------------------------------------------------------------------------
-- 4. matchup_by_aspect: aggregate matchup stats by
--    (leader_a + aspect_a) vs (leader_b + aspect_b)
--    Deduplicates to the latest retrieved_at per matchup pair before aggregating.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.matchup_by_aspect AS
WITH deduped AS (
    SELECT *
    FROM gold.matchup_snapshot
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY archetype_a_id, archetype_b_id, format, start_week, end_week, source
        ORDER BY retrieved_at DESC
    ) = 1
)
SELECT
    aa.leader_id                                      AS leader_a_id,
    aa.leader_name                                    AS leader_a_name,
    aa.base_aspect                                    AS aspect_a,
    ab.leader_id                                      AS leader_b_id,
    ab.leader_name                                    AS leader_b_name,
    ab.base_aspect                                    AS aspect_b,
    aa.leader_id || '|' || aa.base_aspect             AS group_a_id,
    ab.leader_id || '|' || ab.base_aspect             AS group_b_id,
    g.format,
    g.start_week,
    g.end_week,
    g.source,
    MAX(g.retrieved_at)                               AS retrieved_at,
    SUM(g.num_wins)                                   AS num_wins,
    SUM(g.num_plays)                                  AS num_plays,
    SUM(g.num_wins)::DOUBLE / NULLIF(SUM(g.num_plays), 0) AS win_rate,
    -- Wilson CI from pooled counts
    CASE WHEN SUM(g.num_plays) > 0 THEN
        (SUM(g.num_wins)::DOUBLE / SUM(g.num_plays)
         + 1.96*1.96 / (2 * SUM(g.num_plays))
         - 1.96 * SQRT((SUM(g.num_wins)::DOUBLE / SUM(g.num_plays)
                        * (1 - SUM(g.num_wins)::DOUBLE / SUM(g.num_plays))
                        + 1.96*1.96 / (4 * SUM(g.num_plays))) / SUM(g.num_plays)))
        / (1 + 1.96*1.96 / SUM(g.num_plays))
    END                                               AS wr_ci_low,
    CASE WHEN SUM(g.num_plays) > 0 THEN
        (SUM(g.num_wins)::DOUBLE / SUM(g.num_plays)
         + 1.96*1.96 / (2 * SUM(g.num_plays))
         + 1.96 * SQRT((SUM(g.num_wins)::DOUBLE / SUM(g.num_plays)
                        * (1 - SUM(g.num_wins)::DOUBLE / SUM(g.num_plays))
                        + 1.96*1.96 / (4 * SUM(g.num_plays))) / SUM(g.num_plays)))
        / (1 + 1.96*1.96 / SUM(g.num_plays))
    END                                               AS wr_ci_high,
    SUM(g.plays_going_first)                          AS plays_going_first,
    SUM(g.wins_going_first)                           AS wins_going_first,
    SUM(g.wins_going_second)                          AS wins_going_second
FROM deduped g
JOIN silver.archetype aa ON aa.archetype_id = g.archetype_a_id
JOIN silver.archetype ab ON ab.archetype_id = g.archetype_b_id
GROUP BY
    aa.leader_id, aa.leader_name, aa.base_aspect,
    ab.leader_id, ab.leader_name, ab.base_aspect,
    g.format, g.start_week, g.end_week, g.source;
