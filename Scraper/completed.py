"""
VLR.gg Scraper

Scrapes match data from VLR.gg event pages.
Extracts: player stats (Performance tab), round-by-round economy (Economy tab).

Usage:
    from scraper import VLRScraper
    scraper = VLRScraper()
    matches = scraper.scrape_event(2700)  # VCT 2026 Kickoff Americas
"""

import re
import time
import json
import os
import requests
from bs4 import BeautifulSoup
from config import (
    RATE_LIMIT, MAX_RETRIES, REQUEST_TIMEOUT, USER_AGENT,
    MAX_ROUNDS, PROGRESS_FILE,
    HALF_STARTS, BONUS_STREAK_LENGTH,
    ECO_LOADOUT_DIFF, GUN_ROUND_LOADOUT
)


class VLRScraper:
    BASE_URL = "https://www.vlr.gg"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.scraped_ids = self._load_progress()

    # ══════════════════════════════════════════════════════════════
    # PROGRESS TRACKING (resume after interruption)
    # ══════════════════════════════════════════════════════════════

    def _load_progress(self):
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, "r") as f:
                return set(json.load(f))
        return set()

    def _save_progress(self):
        with open(PROGRESS_FILE, "w") as f:
            json.dump(list(self.scraped_ids), f)

    def _mark_done(self, match_id):
        self.scraped_ids.add(str(match_id))
        self._save_progress()

    # ══════════════════════════════════════════════════════════════
    # HTTP
    # ══════════════════════════════════════════════════════════════

    def _get(self, url):
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    return resp
                elif resp.status_code == 429:
                    wait = RATE_LIMIT * (attempt + 2)
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"    HTTP {resp.status_code} for {url}")
            except requests.RequestException as e:
                print(f"    Request error: {e}")
                time.sleep(RATE_LIMIT)
        return None

    # ══════════════════════════════════════════════════════════════
    # EVENT SCRAPING — find all match links for an event
    # ══════════════════════════════════════════════════════════════

    def get_event_matches(self, event_id):
        """
        Get all completed match URLs from a VLR.gg event page.
        Returns list of (match_id, match_url) tuples.
        """
        url = f"{self.BASE_URL}/event/matches/{event_id}/?series_id=all"
        resp = self._get(url)
        if not resp:
            print(f"Failed to load event page: {event_id}")
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        matches = []

        # VLR match links are <a> tags with class "match-item" or similar
        # They contain the match URL as href
        for link in soup.select("a.match-item"):
            href = link.get("href", "")
            if not href:
                continue

            # Extract match ID from URL (first numeric segment)
            parts = href.strip("/").split("/")
            if len(parts) >= 1 and parts[0].isdigit():
                match_id = parts[0]
                match_url = f"{self.BASE_URL}{href}" if href.startswith("/") else href

                # Check if match is completed (has scores, not "upcoming")
                score_spans = link.select(".match-item-vs-team-score")
                has_score = any(s.text.strip().isdigit() for s in score_spans)

                # Also check for "LIVE" or "upcoming" indicators to skip
                eta = link.select_one(".match-item-eta")
                is_upcoming = eta and ("from now" in eta.text.lower() or "live" in eta.text.lower())

                if has_score and not is_upcoming:
                    matches.append((match_id, match_url))

        # Deduplicate
        seen = set()
        unique = []
        for mid, murl in matches:
            if mid not in seen:
                seen.add(mid)
                unique.append((mid, murl))

        return unique

    # ══════════════════════════════════════════════════════════════
    # MATCH SCRAPING — scrape a single match (may have multiple maps)
    # ══════════════════════════════════════════════════════════════

    def scrape_match(self, match_url, match_id, event_name="", game_filter=None):
        """
        Scrape a full match page. Returns a list of map results,
        one dict per map played in the series.

        game_filter: if set (e.g. 1, 2, 3), only scrape that specific game number.
        """
        resp = self._get(match_url)
        if not resp:
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        maps_data = []

        # ── Parse header ──────────────────────────────────────────
        team_names = self._parse_team_names(soup)
        if len(team_names) < 2:
            print(f"    Could not find team names for {match_id}")
            return []

        date = self._parse_date(soup)

        # ── Fetch economy tab (separate URL — VLR only includes economy
        #    tables when ?game=all&tab=economy is requested) ──────
        econ_url = f"{self.BASE_URL}/{match_id}/?game=all&tab=economy"
        time.sleep(RATE_LIMIT)
        econ_resp = self._get(econ_url)
        econ_soup = BeautifulSoup(econ_resp.text, "lxml") if econ_resp else None

        # ── Find all map tabs ─────────────────────────────────────
        game_sections = soup.select(".vm-stats-game")

        game_number = 0
        for section in game_sections:
            game_id = section.get("data-game-id", "")
            if game_id == "all":
                continue  # skip the aggregate "All Maps" tab
            game_number += 1
            if game_filter is not None and game_number != game_filter:
                continue  # skip games not matching the filter

            game_key = f"{match_id}_g{game_id}"
            if game_key in self.scraped_ids:
                print(f"    Game {game_id} already scraped — skipping")
                continue

            map_data = self._parse_map_section(
                section, game_id,
                team_names, match_id, date, event_name,
                econ_soup=econ_soup
            )
            if map_data:
                maps_data.append(map_data)
                self._mark_done(game_key)

        return maps_data

    # ══════════════════════════════════════════════════════════════
    # HEADER PARSING
    # ══════════════════════════════════════════════════════════════

    def _parse_team_names(self, soup):
        names = []
        for el in soup.select(".match-header-link-name .wf-title-med"):
            name = el.text.strip()
            if name:
                names.append(name)
        if len(names) < 2:
            # Fallback: try different selector
            for el in soup.select(".match-header-link-name div"):
                name = el.text.strip()
                if name and name not in names:
                    names.append(name)
        return names[:2]

    def _parse_date(self, soup):
        date_el = soup.select_one(".match-header-date .moment-tz-convert")
        if date_el:
            raw = date_el.get("data-utc-ts", "")
            # Format: "2026-02-28 12:00:00" → "2026-02-28"
            return raw.split(" ")[0] if " " in raw else raw
        return ""

    # ══════════════════════════════════════════════════════════════
    # MAP SECTION PARSING
    # ══════════════════════════════════════════════════════════════

    def _parse_map_section(self, section, game_id,
                           team_names, match_id, date, event_name,
                           econ_soup=None):
        """Parse a single map's data from its stats section."""

        result = {
            "match_id": f"{match_id}_g{game_id}",
            "date": date,
            "event": event_name,
            "team_a": team_names[0],
            "team_b": team_names[1],
            "map": "",
            "winner": "",
            "score_a": 0,
            "score_b": 0,
        }

        # ── Map name ──
        # The map name appears in the map selector or within the section
        map_el = section.select_one(".map span")
        if not map_el:
            map_el = section.select_one(".map div")
        if map_el:
            map_text = map_el.text.strip()
            # Clean: sometimes it's "Haven  PICK" or similar
            result["map"] = map_text.split()[0] if map_text else ""

        # ── Scores ──
        scores = section.select(".score")
        if len(scores) >= 2:
            try:
                result["score_a"] = int(scores[0].text.strip())
                result["score_b"] = int(scores[1].text.strip())
            except ValueError:
                pass

        result["winner"] = (
            result["team_a"] if result["score_a"] > result["score_b"]
            else result["team_b"]
        )

        # ── Player stats (Performance tab data) ──
        players_a, players_b = self._parse_player_stats(section)
        result["players_a"] = players_a
        result["players_b"] = players_b

        # Compute team-level aggregates from player stats
        result["fk_fd_diff_a"] = sum(p["fk"] - p["fd"] for p in players_a)
        result["fk_fd_diff_b"] = sum(p["fk"] - p["fd"] for p in players_b)

        result["acs_a"] = (
            sum(p["acs"] for p in players_a) / len(players_a)
            if players_a else 0
        )
        result["acs_b"] = (
            sum(p["acs"] for p in players_b) / len(players_b)
            if players_b else 0
        )

        result["kast_a"] = (
            sum(p["kast"] for p in players_a) / len(players_a)
            if players_a else 0
        )
        result["kast_b"] = (
            sum(p["kast"] for p in players_b) / len(players_b)
            if players_b else 0
        )

        # ── Economy section for this game (from economy soup) ──
        econ_section = self._find_econ_section(econ_soup, game_id)

        # ── Economy summary: pistols, eco, gun wins from VLR's own table ──
        result.update(self._parse_economy_summary(econ_section))

        # ── Round-by-round data ──
        rounds = self._parse_rounds(econ_section)
        result["rounds"] = rounds

        # ── Override eco/gun with loadout-threshold definitions when data is
        #    available (HTML comments in economy cells contain loadout values).
        #    Falls back to VLR's table values if no loadout data found. ──
        result.update(self._derive_economy_stats(rounds))

        # ── Bonus stats: winning all BONUS_STREAK_LENGTH rounds from each half start ──
        result.update(self._derive_bonus_stats(rounds))

        return result

    # ══════════════════════════════════════════════════════════════
    # PLAYER STATS PARSING
    # ══════════════════════════════════════════════════════════════

    def _parse_player_stats(self, section):
        """
        Parse the performance table within a map section.
        Returns (players_a, players_b) — each a list of player dicts.
        """
        players_a = []
        players_b = []

        # VLR has two tables per map section, one per team
        tables = section.select("table.wf-table-inset")
        if len(tables) < 2:
            # Fallback: try looking for the stats container
            tables = section.select(".vm-stats-game-table")

        for t_idx, table in enumerate(tables[:2]):
            players = []
            rows = table.select("tbody tr")

            for row in rows:
                player = self._parse_player_row(row)
                if player:
                    players.append(player)

            if t_idx == 0:
                players_a = players
            else:
                players_b = players

        return players_a, players_b

    def _parse_player_row(self, row):
        """Parse a single player's stats from a table row."""
        player = {
            "name": "", "team": "", "agent": "",
            "acs": 0.0, "kills": 0, "deaths": 0, "assists": 0,
            "fk": 0, "fd": 0, "kast": 0.0,
        }

        # Player name
        name_el = row.select_one("td.mod-player .text-of")
        if name_el:
            player["name"] = name_el.text.strip()
        else:
            return None  # Skip non-player rows

        # Agent
        agent_img = row.select_one("td.mod-agents img")
        if agent_img:
            player["agent"] = agent_img.get("alt", "") or agent_img.get("title", "")

        # VLR.gg table column layout (all use class "mod-stat"):
        #   idx 0: Rating, idx 1: ACS, idx 2: Kills, idx 3: Deaths,
        #   idx 4: Assists, idx 5: K/D diff, idx 6: KAST, idx 7: ADR,
        #   idx 8: HS%, idx 9: FK (mod-fb), idx 10: FD (mod-fd), idx 11: FK diff
        stat_tds = row.select("td.mod-stat")

        def _val_stat(idx):
            """Get mod-both text (or td text) from the Nth td.mod-stat."""
            if idx >= len(stat_tds):
                return ""
            td = stat_tds[idx]
            el = td.select_one(".mod-both") or td
            parts = el.text.strip().split()
            return parts[0] if parts else ""

        def _val(td_selector):
            """Get mod-both text (or td text) for a CSS-selected td."""
            el = (row.select_one(f"{td_selector} .mod-both")
                  or row.select_one(td_selector))
            if not el:
                return ""
            parts = el.text.strip().split()
            return parts[0] if parts else ""

        try:
            v = _val_stat(1)  # ACS — positional (no unique class)
            if v: player["acs"] = float(v)
        except ValueError:
            pass
        try:
            v = _val("td.mod-vlr-kills")
            if v: player["kills"] = int(v)
        except ValueError:
            pass
        try:
            v = _val("td.mod-vlr-deaths")
            if v: player["deaths"] = int(v)
        except ValueError:
            pass
        try:
            v = _val("td.mod-vlr-assists")
            if v: player["assists"] = int(v)
        except ValueError:
            pass
        try:
            v = _val_stat(6)  # KAST — positional (no unique class)
            if v: player["kast"] = float(v.replace("%", ""))
        except ValueError:
            pass
        try:
            v = _val("td.mod-fb")  # FK — VLR calls this "First Blood"
            if v: player["fk"] = int(v)
        except ValueError:
            pass
        try:
            v = _val("td.mod-fd")  # FD
            if v: player["fd"] = int(v)
        except ValueError:
            pass

        return player

    # ══════════════════════════════════════════════════════════════
    # ECONOMY TAB HELPERS
    # ══════════════════════════════════════════════════════════════

    def _find_econ_section(self, econ_soup, game_id):
        """Find the vm-stats-game section for game_id in the economy soup."""
        if not econ_soup:
            return None
        for sec in econ_soup.select(".vm-stats-game"):
            if sec.get("data-game-id") == str(game_id):
                return sec
        return None

    def _parse_economy_summary(self, econ_section):
        """
        Parse the mod-overview table from the Economy tab.
        Extracts pistols won, eco won, and gun rounds won directly
        from VLR's own summary (avoids loadout-threshold guesswork).

        Table layout (teams as rows, stats as columns):
          header: [team] [Pistol Won] [Eco (won)] [$ (won)] [$$ (won)] [$$$ (won)]
          row 0:  team A values
          row 1:  team B values
        """
        stats = {
            "pistols_won_a": 0, "pistols_won_b": 0,
            "eco_won_a": 0,     "eco_won_b": 0,
            "gun_won_a": 0,     "gun_won_b": 0,
        }
        if not econ_section:
            return stats

        # First mod-econ table is the economy summary (Pistol / Eco / Gun tiers)
        econ_tables = econ_section.select("table.wf-table-inset.mod-econ")
        table = econ_tables[0] if econ_tables else None
        if not table:
            return stats

        def extract_won(text):
            """'1' → 1,  '3 (1)' → 1  (wins are in parens when >0 total)"""
            m = re.search(r'\((\d+)\)', text.strip())
            if m:
                return int(m.group(1))
            try:
                return int(text.strip().split()[0])
            except (ValueError, IndexError):
                return 0

        # Find column indices from header row
        col = {}
        for row in table.select("tr"):
            cells = row.select("th, td")
            texts = [c.get_text(strip=True) for c in cells]
            if any("Pistol" in t for t in texts):
                for i, t in enumerate(texts):
                    if "Pistol" in t:
                        col["pistol"] = i
                    elif "Eco" in t:
                        col["eco"] = i
                    elif "$$$" in t:
                        col["gun"] = i
                break

        # Default column positions if header not found
        if not col:
            col = {"pistol": 1, "eco": 2, "gun": 5}

        # Map col-key → result field name
        field = {"pistol": "pistols_won", "eco": "eco_won", "gun": "gun_won"}

        # Parse the two team data rows (skip header row which contains "Pistol")
        data_rows = [
            r for r in table.select("tr")
            if r.select("td") and not any(
                "Pistol" in c.get_text() for c in r.select("td, th")
            )
        ]
        for team_idx, row in enumerate(data_rows[:2]):
            suffix = "a" if team_idx == 0 else "b"
            cells = row.select("td")
            for key, idx in col.items():
                if idx < len(cells):
                    stats[f"{field[key]}_{suffix}"] = extract_won(cells[idx].get_text())

        return stats

    # ══════════════════════════════════════════════════════════════
    # ROUND-BY-ROUND PARSING (Economy tab)
    # ══════════════════════════════════════════════════════════════

    def _parse_rounds(self, econ_section):
        """
        Parse round-by-round data from table.wf-table-inset.mod-econ[1].
        Each td is one round; each td contains two div.rnd-sq elements
        (one per team, in header order). The winner has class mod-win.
        Loadout values are stored in the title attribute of div.rnd-sq.
        """
        rounds = []
        if not econ_section:
            return rounds

        # Two mod-econ tables per map: [0] summary/legend, [1] round-by-round
        econ_tables = econ_section.select("table.wf-table-inset.mod-econ")
        if len(econ_tables) < 2:
            return rounds

        def parse_bank(text):
            try:
                return int(float(text.strip().lower().replace("k", "")) * 1000)
            except ValueError:
                return None

        def parse_title(el):
            """Read loadout from rnd-sq title attribute (e.g. title='22650')."""
            try:
                return int(el.get("title", ""))
            except (ValueError, TypeError):
                return None

        round_num = 1
        for cell in econ_tables[1].select("td"):
            # The header cell contains team names and "(BANK)" labels — skip it
            if "BANK" in cell.get_text():
                continue
            if round_num > MAX_ROUNDS:
                break

            # Each round cell has exactly two div.rnd-sq: [0]=team A, [1]=team B
            rnd_sq_els = cell.select("div.rnd-sq")
            if len(rnd_sq_els) < 2:
                continue

            rnd_sq_a, rnd_sq_b = rnd_sq_els[0], rnd_sq_els[1]

            # Winner: whichever rnd-sq carries the mod-win class
            classes_a = rnd_sq_a.get("class", [])
            classes_b = rnd_sq_b.get("class", [])
            if "mod-win" in classes_a:
                winner = "a"
            elif "mod-win" in classes_b:
                winner = "b"
            else:
                round_num += 1
                continue  # Round with no winner recorded — skip

            # Loadouts from title attribute
            loadout_a = parse_title(rnd_sq_a)
            loadout_b = parse_title(rnd_sq_b)

            # Bank balances (two div.bank per cell)
            banks = cell.select("div.bank")
            bank_a = parse_bank(banks[0].get_text()) if len(banks) >= 1 else None
            bank_b = parse_bank(banks[1].get_text()) if len(banks) >= 2 else None

            rounds.append({
                "round": round_num,
                "winner": winner,
                "loadout_a": loadout_a,
                "loadout_b": loadout_b,
                "bank_a": bank_a,
                "bank_b": bank_b,
            })
            round_num += 1

        return rounds

    # ══════════════════════════════════════════════════════════════
    # ECONOMY STATS (derived from round-by-round loadout data)
    # ══════════════════════════════════════════════════════════════

    def _derive_economy_stats(self, rounds):
        """
        Compute eco_won and gun_won from round-by-round loadout data.

        Eco round  : abs(loadout_a - loadout_b) >= ECO_LOADOUT_DIFF (12 500).
                     The eco team is whichever side has the smaller loadout.
                     eco_won_X counts rounds where team X was the eco side and won.

        Gun round  : both teams' loadout >= GUN_ROUND_LOADOUT (16 000).
                     gun_won_X counts gun rounds won by team X.

        If no rounds have loadout data (HTML comments absent on this match),
        returns {} so the caller keeps VLR's own summary-table values.
        """
        rounds_with_data = [
            r for r in rounds
            if r.get("loadout_a") is not None and r.get("loadout_b") is not None
        ]
        if not rounds_with_data:
            return {}

        stats = {"eco_won_a": 0, "eco_won_b": 0, "gun_won_a": 0, "gun_won_b": 0}
        for r in rounds_with_data:
            la = r["loadout_a"]
            lb = r["loadout_b"]
            winner = r.get("winner")

            if la >= GUN_ROUND_LOADOUT and lb >= GUN_ROUND_LOADOUT:
                # Both teams fully bought — gun round
                if winner == "a":
                    stats["gun_won_a"] += 1
                elif winner == "b":
                    stats["gun_won_b"] += 1
            elif abs(la - lb) >= ECO_LOADOUT_DIFF:
                # One side on eco — credit the win to the eco team only
                if la < lb and winner == "a":
                    stats["eco_won_a"] += 1
                elif lb < la and winner == "b":
                    stats["eco_won_b"] += 1

        return stats

    # ══════════════════════════════════════════════════════════════
    # BONUS STATS (derived from round-by-round data)
    # ══════════════════════════════════════════════════════════════

    def _derive_bonus_stats(self, rounds):
        """
        Compute bonus_won (winning all BONUS_STREAK_LENGTH rounds from
        each half start) from round-by-round data.
        """
        stats = {"bonus_won_a": 0, "bonus_won_b": 0}
        if not rounds:
            return stats

        rmap = {r["round"]: r for r in rounds}
        for half_start in HALF_STARTS:
            streak_a = all(
                rmap.get(half_start + offset, {}).get("winner") == "a"
                for offset in range(BONUS_STREAK_LENGTH)
            )
            streak_b = all(
                rmap.get(half_start + offset, {}).get("winner") == "b"
                for offset in range(BONUS_STREAK_LENGTH)
            )
            if streak_a:
                stats["bonus_won_a"] += 1
            if streak_b:
                stats["bonus_won_b"] += 1

        return stats

    # ══════════════════════════════════════════════════════════════
    # FULL EVENT SCRAPE
    # ══════════════════════════════════════════════════════════════

    def scrape_event(self, event_id, event_name=""):
        """
        Scrape all matches from a VLR.gg event.
        Returns list of map result dicts.
        """
        print(f"\n{'═' * 60}")
        print(f"  Scraping: {event_name}")
        print(f"  Event ID: {event_id}")
        print(f"{'═' * 60}")

        match_list = self.get_event_matches(event_id)
        print(f"  Found {len(match_list)} completed matches")

        all_maps = []
        for i, (mid, murl) in enumerate(match_list):
            if str(mid) in self.scraped_ids:
                print(f"  [{i+1}/{len(match_list)}] {mid} — already scraped, skipping")
                continue

            print(f"  [{i+1}/{len(match_list)}] Scraping {mid}...")
            maps = self.scrape_match(murl, mid, event_name)
            all_maps.extend(maps)
            self._mark_done(mid)

            # Rate limit
            time.sleep(RATE_LIMIT)

        print(f"  Done: {len(all_maps)} maps scraped from {event_name}")
        return all_maps
