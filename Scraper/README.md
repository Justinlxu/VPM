# VLR.gg → Excel Scraper

Scrapes Valorant VCT match data from VLR.gg and writes it to a structured 3-sheet Excel workbook for ML model training.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Scrape all events listed in config.py (2022-2026, will take hours)
python main.py

# Scrape a single event
python main.py --event 2700

# Resume after interruption (skips already-scraped matches)
python main.py --resume

# Start fresh (deletes progress + output file)
python main.py --reset

# Debug: inspect VLR.gg HTML structure to fix selectors
python main.py --inspect
```

## Output: valorant_data.xlsx

### Sheet 1 — Model Features (one row per map)
| Column | Description |
|---|---|
| Match ID | Unique ID from VLR |
| Date | Match date |
| Patch | Game patch version |
| Map | Map name |
| Team A / Team B | Team names (for reference) |
| Winner | Which team won |
| Score A / Score B | Round scores |
| FK/FD Diff A / B | Sum of (first kills − first deaths) across all 5 players |
| Pistols Won A / B | Pistol rounds won (out of 2) |
| ACS A / B | Average Combat Score (mean of 5 players) |
| KAST A / B | Kill/Assist/Survive/Trade % (mean of 5 players) |
| Bonus Won A / B | 3-0 half starts (max 2 per map) |
| Eco Won A / B | Rounds won with 12,500+ loadout disadvantage |
| Gun Rounds Won A / B | Rounds where both teams had 16,000+ loadout |

### Sheet 2 — Round Data (one row per map, matches Sheet 1)
96 data columns: for each of 32 rounds, stores Winner, Loadout A, Loadout B.
Unused rounds left blank.

### Sheet 3 — Player Stats (10 rows per map)
| Column | Description |
|---|---|
| Match ID | Links to Sheet 1 |
| Player Name | In-game name |
| Team | Team name |
| Agent | Agent played |
| ACS, Kills, Deaths, Assists, FK, FD, KAST | Individual stats |

## Economy Definitions (edit in config.py)

- **Eco round:** 12,500+ credits loadout difference
- **Gun round:** Both teams have 16,000+ loadout
- **Bonus:** Won first 3 rounds of a half (pistol → buy → bonus)

## Important Notes

### First Run — Inspect Before Bulk Scraping
VLR.gg's HTML changes occasionally. Before scraping everything:
```bash
python main.py --inspect
```
Paste a recent match URL and verify the selectors are finding the right elements. If not, update the CSS selectors in `scraper.py`.

### Rate Limiting
The scraper waits 2 seconds between requests by default. A full scrape of 2022-2026 data will take several hours. It saves progress and can resume if interrupted.

### Event IDs
Event IDs in `config.py` may need updating. Find the correct ID from the VLR.gg URL:
```
https://www.vlr.gg/event/2700/vct-2026-kickoff-americas
                          ^^^^
                          This is the event ID
```

### Loadout Data
Loadout values (needed for eco/gun round classification) appear on hover on VLR.gg. They may be stored as `data-` attributes in the HTML. If the scraper can't find loadout values, it falls back to bank values. Run `--inspect` to verify.
