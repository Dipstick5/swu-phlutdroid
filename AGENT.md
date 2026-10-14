# SWU Meta Agent

An agent that answers up-to-date questions about Star Wars: Unlimited (SWU): card rules, current meta, matchups, card performance. It's backed by a small local data store and exposed to Claude as an MCP server.

## Constraints

- **Free and maintainable.** No paid infrastructure, no servers to patch. If a choice adds ongoing upkeep, push back.
- Personal/non-commercial project.
- Prefer minimal configuration. Add a dependency only when it removes real work.

## Stack

```
GitHub Actions (daily cron)
  -> Python ingest: pull SWUStats APIs + card data
  -> DuckDB file (bronze / silver / gold schemas)
  -> commit to repo or publish as release asset
MCP server (Python, stdio, run locally)
  -> reads the DuckDB file
  -> exposes the agent tools listed below
```

Rejected: Databricks Free Edition. Outbound internet is restricted to trusted domains (can't reach swustats.net), Apps stop after 24h, and inactive accounts can be deleted.

## Data sources

### SWUStats (primary meta source)

Undocumented JSON APIs behind https://swustats.net/TCGEngine/Stats/DeckMetaStats.php. Both are plain GETs and need no auth.

**Matchups for one archetype:**
```
GET https://swustats.net/TCGEngine/APIs/DeckMetaMatchupStatsAPI.php
    ?leaderID=LAW_008&baseID=JTL_020&startWeek=55&endWeek=55&consolidate=1&format=premier
```
Returns an array of:
```
opponentLeaderID, opponentBaseID, numWins, numPlays, playsGoingFirst,
winsGoingFirst, winsGoingSecond, turnsInWins, totalTurns,
cardsResourcedInWins, totalCardsResourced, remainingHealthInWins
```

**Archetype-level meta table:** the page also loads a deck-level endpoint, most likely `DeckMetaStatsAPI.php` with the same params. This hasn't been confirmed. Open the page with network capture and verify the endpoint name and response shape before building on it. Columns shown on the page: leader, base, num plays, win rate, avg turns in wins/losses, avg cards resourced in wins, avg remaining health in wins.

**Other pages to investigate:** `Stats/CardMetaStats.php` (card-level stats), `Stats/MeleeTournaments.php` (paper events), and the `APIs` nav link.

**Week numbering:** Week 0 = Sep 20-26, 2025. Week 55 = Oct 10-16, 2026. Weeks run Sat-Fri. Formats: `premier`, plus Eternal, Twin Suns, Team Suns, Padawan, and preview variants.

**Etiquette:** contact the SWUStats maintainer (via Discord) before running daily pulls. Ask about rate limits or whether an export exists. Cache aggressively. Never hammer the endpoints.

### Where SWUStats data comes from (bias profile)

Karabast (the online SWU client, repo `SWU-Karabast/forceteki`) pushes game results to SWUStats in `server/utils/statHandlers/SwuStatsHandler.ts`, and to SWUBase in `SwuBaseHandler.ts`. Gating is in `server/gamenode/Lobby.ts`. A game is sent only when **all** of these hold:
- the format is Premier
- the game reached round 2 or later
- it isn't a draw
- at least one player loaded their deck from SWUStats (for SWUBase: a linked account)

Consequences:
- Win rates are for **tracked, engaged players vs. the general queue**, so most popular decks show >50%. Compare archetypes to each other; don't present absolute win rates as true edges.
- Two tracked players can report the same game from both sides. Head-to-head numbers from A's perspective and B's perspective are separate samples. Don't sum them.
- The Karabast payload includes per-card metrics (drawn/played/discarded) and per-turn metrics (cards used, resources used/left, damage dealt/taken). Check whether SWUStats exposes them; `card_performance` depends on it.

### Card data

Candidates: card definitions in the Karabast/forceteki repo, swudb.com, and the official card DB. Pick one canonical source for the `Card` dimension.

### Paper tournaments

SWUBase and Melee. Keep paper results **separate** from online games. Never blend the two into one win rate.

## Ontology

### 1. Game rules (slow-changing)
- `Card`: keyed on the **FFG UID** (e.g. `LAW_008`). Never key or join on name. Attributes: name, subtitle, set, number, type, cost, aspects, arena, traits, keywords, power/HP, text.
- `CardPrinting`: variants, linked by `printingOf` to `Card`.
- `Set`: code, name, release date, rotation status per format.
- `Format`: linked by `legalSets` to `Set`.
- `Keyword` / `Mechanic`: rules text. `Card` links to it via `hasKeyword`.
- `Ruling`: links via `about` to `Card` or `Mechanic`. Carries source and date.
- `BanListEntry`: `Card` `suspendedIn` `Format`, with valid_from/valid_to (SCD2).

### 2. Competitive objects
- `Deck`: leader, base, contains{qty} of `Card`.
- `Archetype`: leader + base. This is the unit meta stats attach to.
- `Alias`: natural-language names ("Krennic SGC", "Canto Ahsoka") mapped to `Archetype` or `Card`.

### 3. Observations (time-bound, sourced)
- `MetaSnapshot`: Archetype x Format x Period x Source, holding plays, wins, avg turns, etc.
- `MatchupSnapshot`: (Archetype A, Archetype B) x Format x Period x Source.
- `CardStat` / `TurnStat`: only if available.
- `Tournament` / `Standing`: paper results.

### Cross-cutting rules
- Every observation carries `source`, `period` (valid_from/valid_to), `sample_size`, and `retrieved_at`.
- `Period` is an explicit entity, anchored to set releases and ban updates. "Current" means since the latest set release.
- `Source` is an entity with a stored `bias_note`, which tools return with their data.
- Confidence comes from sample size (Wilson interval). Return `wr_ci` alongside `wr`.

### Medallion layout (DuckDB schemas)
- **bronze**: raw API JSON, one row per pull, stamped with `retrieved_at`.
- **silver**: conformed entities above. `Card` is the dimension. Legality/bans use SCD2.
- **gold**: weekly `meta_snapshot` and `matchup_snapshot`. **Persist every week.** SWUStats only serves aggregates over a chosen window, so our history is the only durable record.

## Agent tools (MCP)

The agent never writes SQL and never guesses IDs. Resolve first, then query.

| Tool | Signature | Notes |
|---|---|---|
| `resolve` | `(text, kind?) -> [{id, name, subtitle, kind, confidence}]` | Alias table, then fuzzy name, then subtitle. **Return all candidates when ambiguous** (e.g. two different "Luke Skywalker" leaders). |
| `card` | `(id) -> {stats, aspects, text, keywords, set, legality[], rulings[], ban_status}` | Rules facts only. No meta data. |
| `meta` | `(format, period="current", min_n=50, sort="plays") -> [{archetype, plays, wr, wr_ci, share, avg_turns_win}]` | Always returns `{source, window, n, bias_note}`. |
| `matchup` | `(archetype_a, archetype_b?, period="current", min_n=8) -> [{opponent, w, l, wr, wr_ci, first_player_split}]` | With no B, returns A's full spread (answers "what does X fold to"). Return both perspectives when both exist. |
| `card_performance` | `(card_id \| archetype_id, period) -> [{card, inclusion_rate, played_rate, wr_when_played, delta}]` | Depends on per-card data availability. |
| `search_docs` | `(query, entity_ids?) -> [{snippet, source, date, url}]` | Vector search over rules, rulings, ban announcements, and set articles. |
| `periods` | `(format) -> [{id, start, end, trigger}]` | Set releases, bans, weeks. |
| `tournaments` | `(format, period, tier?) -> [{event, date, players, top8}]` | Paper only. |

Design rules:
- No `run_sql` tool for the agent.
- Defaults encode correctness: `period="current"` and `min_n` thresholds.
- Caveats travel with the data in every response.

## Failure modes to design out

1. Joining on card names instead of UIDs.
2. Blending online and paper data.
3. Answering without a period, which silently mixes pre- and post-rotation data.
4. Presenting a skewed source's win rates as absolute.

## Known card IDs (seed for the alias table)

Confirmed from the SWUStats UI (week 55, Oct 2026):

| ID | Name | Kind |
|---|---|---|
| LAW_008 | Director Krennic | leader |
| ASH_009 | Ahsoka Tano | leader |
| HMW_014 | Wicket | leader |
| HMW_003 | Doctor Hemlock | leader |
| HMW_010 | Tarfful | leader |
| JTL_006 | Darth Vader | leader |
| LOF_008 | Obi-Wan Kenobi | leader |
| JTL_020 | Shield Generator Complex | base |
| SEC_026 | Mount Tantiss | base |
| SEC_021 | Republic City | base |
| LAW_028 | Canto Bight | base |
| LAW_020 | Daimyo's Palace | base |
| LOF_029 | Crystal Caves | base |

Seen but unidentified: LAW_018 (leader), LAW_022, LAW_019, LAW_025, JTL_028, SEC_024 (bases), LOF_016 / LOF_023. Resolve these from the card source.

Card images: `https://swustats.net/TCGEngine/AppCore/SWU/Images/concat/<ID>.webp`. On the meta page, leader and base names are only available as `<img alt>` attributes.

## Build order

1. Confirm the deck-level meta endpoint and response shape (network capture).
2. Ingest the card dimension from one canonical source.
3. Write the bronze ingest for meta + matchups for the current week. Run it locally.
4. Build silver/gold transforms in DuckDB SQL.
5. Write the MCP server with `resolve`, `meta`, and `matchup`. Test them against the week-55 Krennic vs Ahsoka numbers below.
6. Add the GitHub Actions daily workflow.
7. Then add `card`, `periods`, `search_docs`, `card_performance`, and `tournaments`.

## Sanity check data (week 55, Premier, pulled Oct 14 2026, partial week)

- Krennic / Shield Generator Complex: 310 plays, 59.35% WR
- Ahsoka / Canto Bight: 307 plays, 68.40% WR
- Krennic (all bases) vs Ahsoka, from Krennic players' games: 22-40
- Krennic SGC vs Ahsoka / Mount Tantiss: 2-8
