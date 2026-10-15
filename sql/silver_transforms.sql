-- Silver layer transforms: lift raw JSON into conformed relational tables.
-- Run after bronze ingest. Safe to re-run (CTAS with replace).

CREATE SCHEMA IF NOT EXISTS silver;

-- ---------------------------------------------------------------------------
-- 1. Archetype dimension
--    Unique (leader_id, base_id) pairs with their display names.
--    Derived from meta_raw payload because the matchup API returns only IDs
--    for opponents - we need the name lookup from meta.
-- ---------------------------------------------------------------------------
-- Base aspect lookup: base_id -> aspect color (Heroism/Villainy not yet on bases)
CREATE OR REPLACE TABLE silver.base_aspect (
    base_id     VARCHAR PRIMARY KEY,
    aspect      VARCHAR  -- heroism|villainy|command|aggression|cunning|vigilance|colorless
);

INSERT INTO silver.base_aspect VALUES
    ('IBH_054', 'vigilance'),
    ('JTL_020', 'vigilance'),
    ('JTL_021', 'vigilance'),
    ('JTL_024', 'command'),
    ('JTL_025', 'aggression'),
    ('JTL_028', 'cunning'),
    ('JTL_031', 'colorless'),
    ('LAW_019', 'vigilance'),
    ('LAW_020', 'vigilance'),
    ('LAW_022', 'command'),
    ('LAW_025', 'aggression'),
    ('LAW_028', 'cunning'),
    ('LAW_029', 'cunning'),
    ('LOF_019', 'vigilance'),
    ('LOF_020', 'vigilance'),
    ('LOF_023', 'command'),
    ('LOF_026', 'aggression'),
    ('LOF_029', 'cunning'),
    ('SEC_021', 'command'),
    ('SEC_024', 'aggression'),
    ('SEC_026', 'cunning')
ON CONFLICT (base_id) DO UPDATE SET aspect = excluded.aspect;

CREATE OR REPLACE TABLE silver.archetype AS
SELECT DISTINCT
    r.leaderID                                AS leader_id,
    r.leaderTitle                             AS leader_name,
    r.leaderSubtitle                          AS leader_subtitle,
    r.baseID                                  AS base_id,
    r.baseTitle                               AS base_name,
    r.baseSubtitle                            AS base_subtitle,
    r.leaderID || '/' || r.baseID             AS archetype_id,
    COALESCE(ba.aspect, 'unknown')            AS base_aspect
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
) r
LEFT JOIN silver.base_aspect ba ON ba.base_id = r.baseID;

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
--    Bulk format: each payload item contains leaderID/baseID for both sides.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver.matchup_stat AS
SELECT
    r.id                                              AS raw_id,
    r.start_week,
    r.end_week,
    r.format,
    r.retrieved_at,
    json_extract_string(j, '$.leaderID')              AS archetype_a_leader,
    json_extract_string(j, '$.baseID')                AS archetype_a_base,
    json_extract_string(j, '$.leaderID') || '/' ||
        json_extract_string(j, '$.baseID')            AS archetype_a_id,
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
    -- Specific archetype aliases
    ('Krennic SGC',        'archetype', 'LAW_008/JTL_020', 1.0),
    ('Krennic Shield',     'archetype', 'LAW_008/JTL_020', 1.0),
    ('Ahsoka Canto',       'archetype', 'ASH_009/LAW_028', 1.0),
    ('Canto Ahsoka',       'archetype', 'ASH_009/LAW_028', 1.0),
    -- Leader aliases
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
    -- Base aliases
    ('Shield Generator Complex', 'base', 'JTL_020',         1.0),
    ('SGC',                'base',      'JTL_020',          1.0),
    ('Mount Tantiss',      'base',      'SEC_026',          1.0),
    ('Tantiss',            'base',      'SEC_026',          0.9),
    ('Republic City',      'base',      'SEC_021',          1.0),
    ('Canto Bight',        'base',      'LAW_028',          1.0),
    ('Daimyo''s Palace',   'base',      'LAW_020',          1.0),
    ('Crystal Caves',      'base',      'LOF_029',          1.0),
    -- Aspect-group aliases: leader_id|aspect pseudo-id for aspect-level queries
    ('Krennic blue',       'leader_aspect', 'LAW_008|vigilance',  1.0),
    ('Krennic vigilance',  'leader_aspect', 'LAW_008|vigilance',  1.0),
    ('Ahsoka yellow',      'leader_aspect', 'ASH_009|cunning',    1.0),
    ('Ahsoka cunning',     'leader_aspect', 'ASH_009|cunning',    1.0),
    ('Wicket blue',        'leader_aspect', 'HMW_014|vigilance',  1.0),
    ('Hemlock yellow',     'leader_aspect', 'HMW_003|cunning',    1.0),
    ('Hemlock Tantiss',    'leader_aspect', 'HMW_003|cunning',    1.0),
    ('Tarfful blue',       'leader_aspect', 'HMW_010|vigilance',  1.0),
    ('Obi-Wan yellow',     'leader_aspect', 'LOF_008|cunning',    1.0);

-- ---------------------------------------------------------------------------
-- 5. Card stat (per-archetype card performance)
--    Each row in bronze.card_stat_raw has payload: array of card objects.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver.card_stat AS
SELECT
    r.id                                                      AS raw_id,
    r.leader_id,
    r.base_id,
    r.leader_id || '/' || r.base_id                           AS archetype_id,
    r.start_week,
    r.end_week,
    r.format,
    r.retrieved_at,
    json_extract_string(j, '$.cardId')                        AS card_id,
    json_extract_string(j, '$.cardTitle')                     AS card_name,
    json_extract_string(j, '$.cardType')                      AS card_type,
    TRY_CAST(json_extract_string(j, '$.numPlays')   AS INTEGER) AS num_plays,
    TRY_CAST(json_extract_string(j, '$.numWins')    AS INTEGER) AS num_wins,
    TRY_CAST(json_extract_string(j, '$.winRate')    AS DOUBLE)  AS win_rate,
    TRY_CAST(json_extract_string(j, '$.playRate')   AS DOUBLE)  AS play_rate,
    TRY_CAST(json_extract_string(j, '$.avgCopies')  AS DOUBLE)  AS avg_copies
FROM bronze.card_stat_raw r,
     LATERAL (SELECT unnest(json_extract(r.payload, '$[*]')) AS j)
WHERE r.payload IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. Tournament (Melee event list + per-event standings/decks)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver.tournament AS
SELECT
    TRY_CAST(json_extract_string(j, '$.id') AS INTEGER) AS tournament_id,
    json_extract_string(j, '$.name')            AS name,
    json_extract_string(j, '$.date')            AS tournament_date,
    json_extract_string(j, '$.melee_url')       AS melee_url,
    r.retrieved_at
FROM bronze.tournament_raw r,
     LATERAL (
         SELECT unnest(json_extract(r.payload, '$.tournaments[*]')) AS j
     )
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY json_extract_string(j, '$.id')
    ORDER BY r.retrieved_at DESC
) = 1;

-- Melee deck payload: leader/base are nested objects, deck URL via meleeId
CREATE OR REPLACE TABLE silver.tournament_standing AS
SELECT
    r.tournament_id,
    json_extract_string(j, '$.player')                    AS player_name,
    TRY_CAST(json_extract_string(j, '$.rank') AS INTEGER) AS standing,
    json_extract_string(j, '$.leader.uuid')               AS leader_id,
    -- base.groupUuid is the canonical SWU base ID; fall back to base.uuid
    COALESCE(
        json_extract_string(j, '$.base.groupUuid'),
        json_extract_string(j, '$.base.uuid')
    )                                                      AS base_id,
    CASE
        WHEN json_extract_string(j, '$.leader.uuid') IS NOT NULL
         AND COALESCE(json_extract_string(j, '$.base.groupUuid'),
                      json_extract_string(j, '$.base.uuid')) IS NOT NULL
        THEN json_extract_string(j, '$.leader.uuid') || '/' ||
             COALESCE(json_extract_string(j, '$.base.groupUuid'),
                      json_extract_string(j, '$.base.uuid'))
    END                                                    AS archetype_id,
    json_extract_string(j, '$.leader.name')               AS leader_name_raw,
    json_extract_string(j, '$.base.name')                 AS base_name_raw,
    TRY_CAST(json_extract_string(j, '$.standings.match_wins')   AS INTEGER) AS wins,
    TRY_CAST(json_extract_string(j, '$.standings.match_losses') AS INTEGER) AS losses,
    json_extract_string(j, '$.standings.match_record')    AS match_record,
    -- Deck URL: meleeId UUID maps to melee.gg/Decklist/View/{meleeId}
    CASE WHEN json_extract_string(j, '$.meleeId') IS NOT NULL
              AND json_extract_string(j, '$.meleeId') != 'null'
         THEN 'https://melee.gg/Decklist/View/' || json_extract_string(j, '$.meleeId')
    END                                                    AS deck_url,
    r.retrieved_at
FROM bronze.tournament_deck_raw r,
     LATERAL (SELECT unnest(json_extract(r.payload, '$.decks[*]')) AS j)
WHERE json_extract_string(j, '$.leader.uuid') IS NOT NULL;
