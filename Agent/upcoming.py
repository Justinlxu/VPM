"""
VLR.gg Upcoming Matches Scraper

Scrapes the upcoming matches schedule from vlr.gg/matches.
Returns a list of upcoming match dicts with team names, start time, and event info.

Usage:
    from upcoming import get_upcoming_matches
    matches = get_upcoming_matches()
"""

import re
import time
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

BASE_URL = "https://www.vlr.gg"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Event must contain "vct" AND one of these region keywords.
VCT_REGIONS = ["americas", "emea", "pacific"]

# Events are dropped if any of these words appear in the name.
VCT_EXCLUSIONS = ["challengers", "china", "game changers", "ascension"]


def _is_vct_event(event_text):
    """Return True only for the three main VCT circuits (Americas/EMEA/Pacific)."""
    text = event_text.lower()
    if any(excl in text for excl in VCT_EXCLUSIONS):
        return False
    return "vct" in text and any(region in text for region in VCT_REGIONS)


def get_upcoming_matches(max_pages=3, vct_only=True):
    """
    Scrape upcoming (and live) matches from vlr.gg/matches.

    Parameters
    ----------
    max_pages : int
        How many pages of the /matches feed to scan.
    vct_only : bool
        If True (default), only return matches from VCT Americas / EMEA / Pacific.
        Set to False to return all events.

    Returns list of dicts:
        match_id    - VLR numeric match ID
        match_url   - full URL to the match page
        team_a      - first team name
        team_b      - second team name
        start_time  - UTC datetime, or None if unparseable
        event       - event / tournament name
        stage       - stage / series label (e.g. "Swiss Stage"), may be empty
        is_live     - True if match is currently live
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    matches = []
    for page in range(1, max_pages + 1):
        url = BASE_URL + "/matches" + (f"?page={page}" if page > 1 else "")
        try:
            resp = session.get(url, timeout=15)
        except requests.RequestException as e:
            print(f"  Request error on page {page}: {e}")
            break

        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code} for {url}")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        found = _parse_matches_page(soup)
        if not found:
            break

        matches.extend(found)
        time.sleep(1)

    # Deduplicate by match_id
    seen = set()
    unique = []
    for m in matches:
        if m["match_id"] not in seen:
            seen.add(m["match_id"])
            unique.append(m)

    # Filter to VCT circuits only
    if vct_only:
        unique = [m for m in unique if _is_vct_event(m["event"] + " " + m["stage"])]

    return unique


def _parse_relative_eta(text):
    """Convert a relative ETA string like '5h 20m', '1d 5h', or '1w 2d' to a UTC datetime."""
    total = timedelta()
    for value, unit in re.findall(r"(\d+)\s*(w|d|h|m)", text):
        value = int(value)
        if unit == "w":
            total += timedelta(weeks=value)
        elif unit == "d":
            total += timedelta(days=value)
        elif unit == "h":
            total += timedelta(hours=value)
        elif unit == "m":
            total += timedelta(minutes=value)
    if total == timedelta():
        return None
    return datetime.now(timezone.utc) + total


def _parse_matches_page(soup):
    matches = []

    for link in soup.select("a.match-item"):
        href = link.get("href", "")
        if not href:
            continue

        parts = href.strip("/").split("/")
        if not parts or not parts[0].isdigit():
            continue

        match_id = parts[0]
        match_url = f"{BASE_URL}{href}" if href.startswith("/") else href

        # Detect live vs upcoming
        eta = link.select_one(".match-item-eta")
        eta_text = eta.text.strip().lower() if eta else ""
        is_live = "live" in eta_text

        # Skip truly-ended matches.  The eta showing "Xm ago" is not enough —
        # VLR also uses "Xm ago" on live matches whose listed start time has
        # passed.  Require numeric scores (a completed map count) before
        # dropping, so we don't silently eat a live match.
        score_spans = link.select(".match-item-vs-team-score")
        has_score = any(s.text.strip().isdigit() for s in score_spans)
        if has_score and not is_live:
            continue

        # Team names
        team_divs = link.select(".match-item-vs-team-name")
        if len(team_divs) < 2:
            continue
        team_a = team_divs[0].text.strip()
        team_b = team_divs[1].text.strip()
        if not team_a or not team_b or "tbd" in team_a.lower() or "tbd" in team_b.lower():
            continue

        # Start time -- prefer the relative ETA (.ml-eta) because VLR updates
        # it dynamically when the previous match runs long or ends early.
        # .moment-tz-convert is the originally scheduled time and goes stale.
        start_time = None
        if not is_live:
            ml_eta = link.select_one(".ml-eta")
            if ml_eta:
                start_time = _parse_relative_eta(ml_eta.text.strip())

        if start_time is None:
            ts_el = link.select_one(".moment-tz-convert")
            if ts_el:
                raw_ts = ts_el.get("data-utc-ts", "")
                if raw_ts:
                    try:
                        start_time = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        pass

        # Event name and stage.
        # VLR structure inside .match-item-event:
        #   <div class="match-item-event-series">Group Stage-Week 1</div>
        #   VCT 2026: EMEA Stage 1          ← direct text node (event name)
        event = ""
        stage = ""
        event_el = link.select_one(".match-item-event")
        if event_el:
            series_el = event_el.select_one(".match-item-event-series")
            if series_el:
                stage = series_el.text.strip()
                series_el.decompose()  # remove so it doesn't bleed into event text
            event = event_el.get_text(separator=" ", strip=True)

        matches.append(
            {
                "match_id": match_id,
                "match_url": match_url,
                "team_a": team_a,
                "team_b": team_b,
                "start_time": start_time,
                "event": event,
                "stage": stage,
                "is_live": is_live,
            }
        )

    return matches


if __name__ == "__main__":
    import sys
    vct_only = "--all" not in sys.argv
    matches = get_upcoming_matches(vct_only=vct_only)
    label = "VCT only" if vct_only else "all events"
    print(f"Found {len(matches)} upcoming matches ({label}):\n")
    for m in matches:
        ts = m["start_time"].astimezone().strftime("%Y-%m-%d %H:%M %Z") if m["start_time"] else "unknown time"
        live = "  [LIVE]" if m["is_live"] else ""
        print(f"  {m['team_a']} vs {m['team_b']}{live}")
        print(f"    {ts}  |  {m['event']}" + (f" — {m['stage']}" if m["stage"] else ""))
        print(f"    {m['match_url']}")
        print()
