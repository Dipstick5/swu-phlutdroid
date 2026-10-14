-- Silver layer transforms: lift raw JSON into conformed relational tables.
-- Run after bronze ingest. Safe to re-run (CTAS with replace).

CREATE SCHEMA IF NOT EXISTS silver;

-- ---------------------------------------------------------------------------
-- 1. Archetype dimension
--    Unique (leader_id, base_id) pairs with their display names.
--    Derived from meta_raw payload because the matchup API returns only IDs
--    for opponents - we need the name lookup from meta.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver.archetype AS
SELECT DISTINCT
    r.leaderID                                AS leader_id,
    r.leaderTitle                             AS leader_name,
    r.leaderSubtitle                          AS leader_subtitle,
    r.baseID                                  AS base_id,
    r.baseTitle                               AS base_name,
    r.baseSubtitle                            AS base_subtitle,
    r.leaderID || '/' || r.baseID             AS archetype_id
FROM (
    SELECT
        json_extract_string(j, '$.leaderID')        AS leaderID,
        json_extract_string(j, '$.leaderTitle')     AS leaderTitle,
        json_extract_string(j, '$.leaderSubtitle')  AS leaderSubtitle,
        json_extract_string(j, '$.baseID')          AS baseID,
        json_extract_string(j, '$.baseTitle')       AS baseTitle,
        json_extract_string(j, '$.baseSubtitle')    AS baseSubtitle
    FROM (
        SELECT unnest(json_extract(payload, '$[*]')) AS j
        FROM bronze.meta_raw
    )
) r;

-- ---------------------------------------------------------------------------
-- 2. Meta stat (expanded, typed)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver.meta_stat AS
SELECT
    m.id                                              AS raw_id,
    m.start_week,
    m.end_week,
    m.format,
    m.retrieved_at,
    json_extract_string(j, '$.leaderID')              AS leader_id,
    json_extract_string(j, '$.baseID')                AS base_id,
    json_extract_string(j, '$.leaderID') || '/' ||
        json_extract_string(j, '$.baseID')            AS archetype_id,
    TRY_CAST(json_extract_string(j, '$.numPlays') AS INTEGER)       AS num_plays,
    TRY_CAST(json_extract_string(j, '$.winRate') AS DOUBLE)         AS win_rate,
    TRY_CAST(json_extract_string(j, '$.avgTurnsInWins') AS DOUBLE)  AS avg_turns_win,
    TRY_CAST(json_extract_string(j, '$.avgTurnsInLosses') AS DOUBLE) AS avg_turns_loss,
    TRY_CAST(json_extract_string(j, '$.avgCardsResourcedInWins') AS DOUBLE)
                                                      AS avg_cards_resourced_win,
    TRY_CAST(json_extract_string(j, '$.avgRemainingHealthInWins') AS DOUBLE)
                                                      AS avg_remaining_health_win
FROM bronze.meta_raw m,
     LATERAL (SELECT unnest(json_extract(m.payload, '$[*]')) AS j);

-- ---------------------------------------------------------------------------
-- 3. Matchup stat (expanded, typed)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver.matchup_stat AS
SELECT
    r.id                                              AS raw_id,
    r.start_week,
    r.end_week,
    r.format,
    r.retrieved_at,
    r.leader_id                                       AS archetype_a_leader,
    r.base_id                                         AS archetype_a_base,
    r.leader_id || '/' || r.base_id                   AS archetype_a_id,
    json_extract_string(j, '$.opponentLeaderID')      AS archetype_b_leader,
    json_extract_string(j, '$.opponentBaseID')        AS archetype_b_base,
    json_extract_string(j, '$.opponentLeaderID') || '/' ||
        json_extract_string(j, '$.opponentBaseID')    AS archetype_b_id,
    TRY_CAST(json_extract_string(j, '$.numWins') AS INTEGER)        AS num_wins,
    TRY_CAST(json_extract_string(j, '$.numPlays') AS INTEGER)       AS num_plays,
    TRY_CAST(json_extract_string(j, '$.playsGoingFirst') AS INTEGER) AS plays_going_first,
    TRY_CAST(json_extract_string(j, '$.winsGoingFirst') AS INTEGER)  AS wins_going_first,
    TRY_CAST(json_extract_string(j, '$.winsGoingSecond') AS INTEGER) AS wins_going_second
FROM bronze.matchup_raw r,
     LATERAL (SELECT unnest(json_extract(r.payload, '$[*]')) AS j);

-- ---------------------------------------------------------------------------
-- 4. Alias table seed (for the `resolve` tool)
--    Natural-language names -> archetype_id or leader_id / base_id.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver.alias (
    alias_text   VARCHAR NOT NULL,
    kind         VARCHAR NOT NULL,
    target_id    VARCHAR NOT NULL,
    confidence   DOUBLE NOT NULL DEFAULT 1.0,
    UNIQUE (alias_text, kind)
);

INSERT INTO silver.alias VALUES
    ('Krennic SGC',        'archetype', 'LAW_008/JTL_020', 1.0),
    ('Krennic Shield',     'archetype', 'LAW_008/JTL_020', 1.0),
    ('Ahsoka Canto',       'archetype', 'ASH_009/LAW_028', 1.0),
    ('Canto Ahsoka',       'archetype', 'ASH_009/LAW_028', 1.0),
    ('Director Krennic',   'leader',    'LAW_008',          1.0),
    ('Krennic',            'leader',    'LAW_008',          0.9),
    ('Ahsoka',             'leader',    'ASH_009',          0.9),
    ('Ahsoka Tano',        'leader',    'ASH_009',          1.0),
    ('Wicket',             'leader',    'HMW_014',          1.0),
    ('Doctor Hemlock',     'leader',    'HMW_003',          1.0),
    ('Hemlock',            'leader',    'HMW_003',          0.9),
    ('Tarfful',            'leader',    'HMW_010',          1.0),
    ('Darth Vader',        'leader',    'JTL_006',          1.0),
    ('Vader',              'leader',    'JTL_006',          0.9),
    ('Obi-Wan Kenobi',     'leader',    'LOF_008',          1.0),
    ('Obi-Wan',            'leader',    'LOF_008',          0.9),
    ('Shield Generator Complex', 'base', 'JTL_020',         1.0),
    ('SGC',                'base',      'JTL_020',          1.0),
    ('Mount Tantiss',      'base',      'SEC_026',          1.0),
    ('Tantiss',            'base',      'SEC_026',          0.9),
    ('Republic City',      'base',      'SEC_021',          1.0),
    ('Canto Bight',        'base',      'LAW_028',          1.0),
    ('Daimyo''s Palace',   'base',      'LAW_020',          1.0),
    ('Crystal Caves',      'base',      'LOF_029',          1.0);
