"""
VLR.gg Match Status Poller

Polls a VLR.gg match page to detect map state transitions:
    upcoming -> live (match started, Map 1 entry closes)
    score appears  (map is final, cooldown begins for next map)

Usage:
    from poller import poll_match_status
    status = poll_match_status("https://www.vlr.gg/123456/...")
"""

import re
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
            "is_live":     bool,   # match page shows the series is underway
            "is_final":    bool,   # entire series is complete
            "maps_to_win": int,    # 2 for Bo3, 3 for Bo5 (default 2 if undetected)
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


def _parse_veto_picks(soup):
    """Extract map picks in order from VLR's veto summary text.

    VLR posts the full veto sequence in .match-header-note as soon as veto
    completes — well before .vm-stats-game sections get real map names.
    Format: "TeamA ban X; TeamB ban Y; TeamA pick Map1; TeamB pick Map2;
             TeamA ban Z; TeamB ban W; Map3 remains"
    Returns a list of map names in map-number order, or [] if no veto yet.
    """
    el = soup.select_one(".match-header-note")
    if not el:
        return []
    text = el.text.strip()
    picks = []
    for part in text.split(";"):
        part = part.strip()
        low = part.lower()
        if " pick " in low:
            picks.append(part.split(" pick ", 1)[1].strip())
        elif low.endswith(" remains"):
            picks.append(part[: -len(" remains")].strip())
    return picks


def _parse_match_page(soup):
    # ── Match-level live / final status ──────────────────────
    # VLR shows a note near the match header: "LIVE", "final", or nothing
    is_live  = False
    is_final = False
    maps_to_win = 2  # Bo3 default; overridden below if a "BoN" note is present

    # VLR renders two .match-header-vs-note elements: one for status
    # ("live"/"final"/relative-eta), one for format ("Bo3"/"Bo5"). Order isn't
    # guaranteed, so classify each by content.
    for note in soup.select(".match-header-vs-note"):
        note_text = note.text.strip()
        m = re.match(r"^Bo(\d+)$", note_text, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if n >= 1:
                maps_to_win = n // 2 + 1  # Bo3 -> 2, Bo5 -> 3, Bo7 -> 4
            continue
        low = note_text.lower()
        if "live" in low:
            is_live = True
        elif "final" in low or "completed" in low:
            is_final = True

    # Fallback: if any map has a score, the match has started
    # (catches cases where the note element has a different class)

    # Veto picks in map order (used as the primary source for map_name since
    # .vm-stats-game .map span shows "TBD" until the map actually loads in-game)
    veto_picks = _parse_veto_picks(soup)

    # ── Build data-game-id -> map_num from the nav tabs ──────
    # VLR sorts .vm-stats-game sections by data-game-id ascending, NOT by map
    # order. When a map's game-id was assigned later than later-scheduled maps
    # (e.g. Pearl with id 269000 between maps with ids 265477-265481), counting
    # DOM position mis-numbers that map. The .vm-stats-gamesnav-item tabs are
    # rendered in true map order — use them as the authority.
    gid_to_map_num = {}
    next_map_num = 1
    for nav in soup.select(".vm-stats-gamesnav-item"):
        classes = nav.get("class", []) or []
        if "mod-all" in classes:
            continue
        gid = nav.get("data-game-id", "")
        if gid:
            gid_to_map_num[gid] = next_map_num
            next_map_num += 1

    # ── Per-map scores ────────────────────────────────────────
    maps = {}
    fallback_counter = 0

    for section in soup.select(".vm-stats-game"):
        game_id = section.get("data-game-id", "")
        if game_id == "all":
            continue
        if game_id in gid_to_map_num:
            game_number = gid_to_map_num[game_id]
        else:
            # Defensive: nav missing for this gid. Fall back to DOM order.
            fallback_counter += 1
            game_number = fallback_counter

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
        # Treat placeholder text as no map name
        if map_name and map_name.lower() in ("tbd", "tba"):
            map_name = None
        # Fall back to the veto pick for this map if the section hasn't
        # populated yet (happens pre-match and during early map loading)
        if not map_name and len(veto_picks) >= game_number:
            map_name = veto_picks[game_number - 1]

        scores = section.select(".score")
        score_a, score_b = 0, 0
        if len(scores) >= 2:
            try:
                score_a = int(scores[0].text.strip())
                score_b = int(scores[1].text.strip())
            except ValueError:
                pass

        # A map is final when one team reaches 13+ AND leads by at least 2.
        # Regulation: 13-X where X <= 11. Overtime: 12-12, then first to 2-round lead.
        high = max(score_a, score_b)
        final = high >= 13 and abs(score_a - score_b) >= 2

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

    # Series is final when one team reaches the win threshold (Bo3: 2, Bo5: 3).
    wins_a = sum(1 for m in maps.values() if m["final"] and m["score_a"] > m["score_b"])
    wins_b = sum(1 for m in maps.values() if m["final"] and m["score_b"] > m["score_a"])
    if wins_a >= maps_to_win or wins_b >= maps_to_win:
        is_final = True

    # Sort by map_num so downstream consumers iterating .items() see Map 1, 2, 3...
    # even when VLR's DOM order doesn't match map order.
    maps = {n: maps[n] for n in sorted(maps)}

    return {
        "is_live":     is_live,
        "is_final":    is_final,
        "maps_to_win": maps_to_win,
        "maps":        maps,
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
