"""
Polymarket Market Fetcher

Fetches live Valorant map market prices from Polymarket's public Gamma API.
No API key required for reads.

Usage:
    from polymatches import get_match_prices
    result = get_match_prices("Fnatic", "ULF Esports")

    # Discover all live Valorant markets:
    python polymatches.py

    # Look up a specific matchup:
    python polymatches.py "Fnatic" "ULF Esports"
"""

import sys
import requests
from difflib import SequenceMatcher

GAMMA_BASE = "https://gamma-api.polymarket.com"


# ══════════════════════════════════════════════════════════════
# STRING MATCHING HELPERS
# ══════════════════════════════════════════════════════════════

def _similarity(a, b):
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _normalize(name):
    """Strip common org suffixes for looser matching."""
    name = name.lower().strip()
    for suffix in [" esports club", " esports", " gaming", " e-sports", " club", " team"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


# Team name aliases: VLR name -> set of alternative names used on Polymarket
TEAM_ALIASES = {
    "eternal fire": {"ulf"},
}


def _team_score(vlr_name, poly_name):
    """Score how well a VLR team name matches a Polymarket team name (0–1)."""
    n_vlr  = _normalize(vlr_name)
    n_poly = _normalize(poly_name)
    if n_vlr == n_poly:
        return 1.0
    # Check aliases
    if n_vlr in TEAM_ALIASES and n_poly in TEAM_ALIASES[n_vlr]:
        return 1.0
    if n_vlr in n_poly or n_poly in n_vlr:
        return 0.9
    return _similarity(n_vlr, n_poly)


# ══════════════════════════════════════════════════════════════
# TITLE PARSING
# Event titles look like:
#   "Valorant: Team A vs Team B (BO3) - VCT EMEA Group Alpha"
# ══════════════════════════════════════════════════════════════

def _parse_event_title(title):
    """
    Extract (team_a, team_b) from a Polymarket Valorant event title.
    Returns (None, None) if the format is unexpected.
    """
    # Strip "Valorant: " prefix
    t = title
    if t.lower().startswith("valorant:"):
        t = t[len("valorant:"):].strip()

    if " vs " not in t:
        return None, None

    left, right = t.split(" vs ", 1)
    team_a = left.strip()

    # Right side: "Team B (BO3) - Event Name" -> strip from " (" onward
    paren = right.find(" (")
    team_b = right[:paren].strip() if paren != -1 else right.split(" - ")[0].strip()

    return team_a, team_b


# ══════════════════════════════════════════════════════════════
# GAMMA API
# ══════════════════════════════════════════════════════════════

def fetch_valorant_events(active_only=True):
    """
    Fetch all Valorant events from the Gamma API (tag_slug=esports, filter by title).
    Returns list of event dicts that have Valorant in the title.
    """
    params = {"tag_slug": "esports", "limit": 200}
    if active_only:
        params["active"] = "true"
        params["closed"] = "false"

    resp = requests.get(f"{GAMMA_BASE}/events", params=params, timeout=15)
    resp.raise_for_status()
    all_events = resp.json()

    return [e for e in all_events if "valorant" in e.get("title", "").lower()]


# ══════════════════════════════════════════════════════════════
# MAIN PUBLIC FUNCTION
# ══════════════════════════════════════════════════════════════

def get_match_prices(team_a, team_b, verbose=False):
    """
    Find the Polymarket event for a VLR.gg matchup and return per-map prices.

    Parameters
    ----------
    team_a, team_b : str
        Team names as they appear on VLR.gg.
    verbose : bool
        Print matching details for debugging.

    Returns
    -------
    dict or None
        {
            "event_id":    str,
            "event_title": str,
            "team_a":      str,   # Polymarket name for team_a
            "team_b":      str,   # Polymarket name for team_b
            "maps": {
                1: {
                    "market_id":    str,
                    "condition_id": str,
                    "question":     str,
                    "price_a":      float,
                    "price_b":      float,
                },
                2: { ... },
                3: { ... },   # only present if market exists
            }
        }
        Returns None if no match found with score >= 0.5.
    """
    events = fetch_valorant_events()
    if verbose:
        print(f"Found {len(events)} active Valorant events on Polymarket")

    best_event   = None
    best_score   = 0.0
    best_swapped = False

    for event in events:
        title = event.get("title", "")
        poly_a, poly_b = _parse_event_title(title)
        if not poly_a or not poly_b:
            continue

        score_fwd = (_team_score(team_a, poly_a) + _team_score(team_b, poly_b)) / 2
        score_rev = (_team_score(team_a, poly_b) + _team_score(team_b, poly_a)) / 2
        score = max(score_fwd, score_rev)
        swapped = score_rev > score_fwd

        if verbose:
            print(f"  '{title}'  ->  {score:.2f}")

        if score > best_score:
            best_score   = score
            best_event   = event
            best_swapped = swapped

    if not best_event or best_score < 0.5:
        if verbose:
            print(f"No confident match for '{team_a}' vs '{team_b}' (best={best_score:.2f})")
        return None

    event_title = best_event.get("title", "")
    event_id    = best_event.get("id", "")
    poly_a, poly_b = _parse_event_title(event_title)

    # Re-orient so team_a/team_b match the caller's order
    if best_swapped:
        poly_a, poly_b = poly_b, poly_a

    if verbose:
        print(f"\nMatched: '{event_title}'  (score={best_score:.2f}, swapped={best_swapped})")

    # ── Find map markets (inline in event["markets"]) ──
    map_markets = {}
    for market in best_event.get("markets", []):
        if market.get("closed", False):
            continue
        question = market.get("question", "")
        q_lower  = question.lower()
        for n in [1, 2, 3]:
            if f"map {n} winner" in q_lower and n not in map_markets:
                map_markets[n] = market
                break

    if not map_markets:
        if verbose:
            print("No open map markets found in this event")
        return None

    # ── Build result ──
    result = {
        "event_id":    str(event_id),
        "event_title": event_title,
        "team_a":      poly_a,
        "team_b":      poly_b,
        "swapped":     best_swapped,
        "maps":        {},
    }

    for map_num in sorted(map_markets):
        market = map_markets[map_num]
        try:
            import json
            outcomes = json.loads(market.get("outcomes", "[]"))
        except (ValueError, TypeError):
            continue

        if len(outcomes) < 2:
            continue

        # Use bestAsk (what you'd actually pay to buy each side).
        # Market is for outcome 0 (team listed first in Polymarket title).
        # bestAsk = price to buy outcome 0, bestBid = price to sell outcome 0.
        # For outcome 1: buy price = 1 - bestBid of outcome 0.
        best_ask = float(market.get("bestAsk", 0))
        best_bid = float(market.get("bestBid", 0))

        if best_ask <= 0 or best_bid <= 0:
            # Fall back to outcomePrices if order book is empty
            try:
                prices = [float(p) for p in json.loads(market.get("outcomePrices", "[]"))]
            except (ValueError, TypeError):
                continue
            if len(prices) < 2:
                continue
            price_a, price_b = prices[0], prices[1]
        else:
            # bestAsk = cost to buy team A, 1 - bestBid = cost to buy team B
            price_a = best_ask
            price_b = round(1.0 - best_bid, 4)

        # If we swapped teams, also swap prices.
        if best_swapped:
            outcomes = [outcomes[1], outcomes[0]]
            price_a, price_b = price_b, price_a

        result["maps"][map_num] = {
            "market_id":    str(market.get("id", "")),
            "condition_id": market.get("conditionId", ""),
            "question":     market.get("question", ""),
            "price_a":      round(price_a, 4),
            "price_b":      round(price_b, 4),
        }

        if verbose:
            print(f"  Map {map_num}: {poly_a} {price_a:.1%}  {poly_b} {price_b:.1%}")

    return result


# ══════════════════════════════════════════════════════════════
# DISCOVERY HELPER
# ══════════════════════════════════════════════════════════════

def list_active_valorant_markets(verbose=True):
    """
    Print all active Valorant events that have at least one open map market.
    """
    events = fetch_valorant_events()
    found  = []

    for event in events:
        map_nums = []
        for m in event.get("markets", []):
            if m.get("closed", False):
                continue
            q = m.get("question", "").lower()
            for n in [1, 2, 3]:
                if f"map {n} winner" in q:
                    map_nums.append(n)
                    break

        if map_nums:
            entry = {
                "title":  event.get("title", ""),
                "id":     event.get("id", ""),
                "maps":   sorted(map_nums),
            }
            found.append(entry)
            if verbose:
                print(f"  {entry['title']}  ->  maps {entry['maps']}")

    if verbose:
        print(f"\nTotal: {len(found)} events with open map markets")
    return found


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) == 3:
        res = get_match_prices(sys.argv[1], sys.argv[2], verbose=True)
        if res:
            print(f"\nResult:")
            for map_num, data in res["maps"].items():
                print(
                    f"  Map {map_num}: {res['team_a']} {data['price_a']:.1%}  "
                    f"{res['team_b']} {data['price_b']:.1%}"
                )
    else:
        print("Active Valorant map markets on Polymarket:\n")
        list_active_valorant_markets()
