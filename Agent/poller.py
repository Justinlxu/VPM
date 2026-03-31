"""
VLR.gg Match Status Poller

Polls a VLR.gg match page to detect map state transitions:
    upcoming -> live (match started, Map 1 entry closes)
    score appears  (map is final, cooldown begins for next map)

Usage:
    from poller import poll_match_status
    status = poll_match_status("https://www.vlr.gg/123456/...")
"""

import time
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.vlr.gg"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
POLL_INTERVAL = 60   # seconds between polls
MAP_ENTRY_COOLDOWN = 5 * 60    # 5 min after previous map final -> entry opens
MAP_ENTRY_DURATION = 5 * 60    # entry window stays open for 5 min then closes


# ══════════════════════════════════════════════════════════════
# SINGLE POLL
# ══════════════════════════════════════════════════════════════

def poll_match_status(match_url, session=None):
    """
    Fetch a VLR match page and return its current state.

    Returns
    -------
    dict:
        {
            "is_live":  bool,   # match page shows the series is underway
            "is_final": bool,   # entire series is complete
            "maps": {
                1: {"final": bool, "score_a": int, "score_b": int},
                2: { ... },
                3: { ... },   # only present if map 3 was/is being played
            }
        }
    None if the page failed to load.
    """
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})

    try:
        resp = session.get(match_url, timeout=15)
    except requests.RequestException as e:
        print(f"  [poller] Request error: {e}")
        return None

    if resp.status_code != 200:
        print(f"  [poller] HTTP {resp.status_code} for {match_url}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    return _parse_match_page(soup)


def _parse_match_page(soup):
    # ── Match-level live / final status ──────────────────────
    # VLR shows a note near the match header: "LIVE", "final", or nothing
    is_live  = False
    is_final = False

    note_el = soup.select_one(".match-header-vs-note")
    if note_el:
        note_text = note_el.text.strip().lower()
        if "live" in note_text:
            is_live = True
        elif "final" in note_text or "completed" in note_text:
            is_final = True

    # Fallback: if any map has a score, the match has started
    # (catches cases where the note element has a different class)

    # ── Per-map scores ────────────────────────────────────────
    maps = {}
    game_number = 0

    for section in soup.select(".vm-stats-game"):
        game_id = section.get("data-game-id", "")
        if game_id == "all":
            continue
        game_number += 1

        # Extract map name from the game header
        map_name = None
        map_el = section.select_one(".map span")
        if map_el:
            map_name = map_el.text.strip()
        else:
            map_el = section.select_one(".map div")
            if map_el:
                map_name = map_el.text.strip()
        # Clean up: sometimes the text includes extra info like "PICK"
        if map_name and "\n" in map_name:
            map_name = map_name.split("\n")[0].strip()

        scores = section.select(".score")
        score_a, score_b = 0, 0
        if len(scores) >= 2:
            try:
                score_a = int(scores[0].text.strip())
                score_b = int(scores[1].text.strip())
            except ValueError:
                pass

        # A map is final when one team has reached a winning score.
        # In Valorant, standard maps go to 13 (or 7 in OT); BO5 same rules.
        final = (score_a >= 13 or score_b >= 13) or (
            # Overtime: 13-13 resolved by 2-point lead
            score_a + score_b > 24 and abs(score_a - score_b) >= 2
        )

        maps[game_number] = {
            "final":    final,
            "score_a":  score_a,
            "score_b":  score_b,
            "map_name": map_name,
        }

    # If any map is final (or scores exist), the match is at least live
    if maps and not is_final:
        any_scores = any(m["score_a"] + m["score_b"] > 0 for m in maps.values())
        if any_scores:
            is_live = True

    # Series is final when 2 maps are final (2-0) or 3 maps are final (2-1)
    final_count = sum(1 for m in maps.values() if m["final"])
    if final_count >= 2:
        is_final = True

    return {
        "is_live":  is_live,
        "is_final": is_final,
        "maps":     maps,
    }


# ══════════════════════════════════════════════════════════════
# BLOCKING POLL LOOP (used by the trading agent)
# ══════════════════════════════════════════════════════════════

def watch_match(match_url, on_event, poll_interval=POLL_INTERVAL):
    """
    Poll a match page every `poll_interval` seconds and call `on_event`
    whenever a meaningful state change is detected.

    `on_event(event_type, map_num, status)` is called with:
        event_type : str  — one of:
            "match_live"    match flipped from upcoming to live (Map 1 started)
            "map_final"     a map just got a final score
            "series_final"  entire series is over
        map_num    : int or None
        status     : the full status dict from poll_match_status

    Returns when the series is complete.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    prev = {"is_live": False, "is_final": False, "maps": {}}

    print(f"  [poller] Watching {match_url}")

    while True:
        status = poll_match_status(match_url, session)
        if status is None:
            time.sleep(poll_interval)
            continue

        # ── Transition: match just went live ──
        if status["is_live"] and not prev["is_live"]:
            print("  [poller] Match is now LIVE (Map 1 started)")
            on_event("match_live", None, status)

        # ── Transition: a map just went final ──
        for map_num, map_data in status["maps"].items():
            prev_map = prev["maps"].get(map_num, {})
            if map_data["final"] and not prev_map.get("final", False):
                score = f"{map_data['score_a']}-{map_data['score_b']}"
                print(f"  [poller] Map {map_num} FINAL  ({score})")
                on_event("map_final", map_num, status)

        # ── Transition: series complete ──
        if status["is_final"] and not prev["is_final"]:
            print("  [poller] Series COMPLETE")
            on_event("series_final", None, status)
            return

        prev = status
        time.sleep(poll_interval)


# ══════════════════════════════════════════════════════════════
# CLI (spot-check a live match)
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python poller.py <vlr_match_url>")
        print("Example: python poller.py https://www.vlr.gg/123456/fnatic-vs-ulf-esports...")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Polling: {url}\n")
    result = poll_match_status(url)
    if result:
        print(f"is_live:  {result['is_live']}")
        print(f"is_final: {result['is_final']}")
        for n, m in result["maps"].items():
            status_str = "FINAL" if m["final"] else "live/upcoming"
            map_label = m.get("map_name") or "TBD"
            print(f"  Map {n} ({map_label}): {m['score_a']}-{m['score_b']}  [{status_str}]")
    else:
        print("Failed to load match page.")
