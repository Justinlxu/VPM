"""
Order book diagnostic.

Pulls a live Valorant per-map market from Polymarket and dumps both tokens'
raw order books alongside four candidate price reads so we can see where the
real liquidity sits:

    1. OLD wrong:   bids[0] / asks[0]         (the original trade.py read)
    2. NEW fixed:   max(bids) / min(asks)     (what get_market_price now does)
    3. Last trade:  get_last_trade_price      (sticky, but useful reference)
    4. Gamma:       market.bestBid / bestAsk  (what polymatches uses)

Usage:
    py -m PolyInt.diag_book                    # picks first live per-map market
    py -m PolyInt.diag_book --condition 0x...  # specific conditionId
"""

import argparse
import sys

from PolyInt.polymatches import fetch_valorant_events
from PolyInt.trade import PolyTrader


def _find_first_map_market():
    """Return (event_title, market_dict) for the first live per-map market."""
    events = fetch_valorant_events()
    print(f"Fetched {len(events)} live Valorant events from Gamma\n")

    for event in events:
        title = event.get("title", "")
        for market in event.get("markets", []):
            if market.get("closed", False):
                continue
            q = market.get("question", "").lower()
            if "map" in q and "winner" in q and market.get("conditionId"):
                return title, market

    return None, None


def _find_market_by_condition(condition_id):
    events = fetch_valorant_events()
    for event in events:
        for market in event.get("markets", []):
            if market.get("conditionId") == condition_id:
                return event.get("title", ""), market
    return None, None


def _dump_book(label, book):
    """Pretty-print a raw order book: all bids and asks with their original order."""
    print(f"  {label}")

    bids = list(book.bids or [])
    asks = list(book.asks or [])

    if not bids and not asks:
        print("    (empty book)")
        return

    print(f"    bids ({len(bids)}) [as returned by API]:")
    for i, b in enumerate(bids):
        print(f"      [{i}] price={float(b.price):.4f}  size={float(b.size):.2f}")

    print(f"    asks ({len(asks)}) [as returned by API]:")
    for i, a in enumerate(asks):
        print(f"      [{i}] price={float(a.price):.4f}  size={float(a.size):.2f}")


def _analyze(token_label, token_id, trader, gamma_best_bid, gamma_best_ask):
    print(f"\n=== {token_label} ===")
    print(f"  token_id: {token_id}")

    book = trader.client.get_order_book(token_id)
    _dump_book("raw book:", book)

    # OLD wrong read
    old_bid = float(book.bids[0].price) if book.bids else None
    old_ask = float(book.asks[0].price) if book.asks else None

    # NEW fixed read (what get_market_price now does)
    new_bid = max((float(b.price) for b in book.bids), default=None)
    new_ask = min((float(a.price) for a in book.asks), default=None)

    last_trade = trader.get_last_trade(token_id)

    print("\n  price reads:")
    print(f"    OLD  bids[0] / asks[0]       bid={old_bid}   ask={old_ask}")
    print(f"    NEW  max(bids)/min(asks)     bid={new_bid}   ask={new_ask}")
    print(f"    last_trade_price             {last_trade}")
    print(f"    Gamma bestBid/bestAsk        bid={gamma_best_bid}   ask={gamma_best_ask}")

    # Sanity check: do the corrected values look consistent with Gamma?
    if new_bid is not None and new_ask is not None and gamma_best_ask is not None:
        mid = (new_bid + new_ask) / 2
        gap = abs(mid - gamma_best_ask)
        verdict = "LOOKS GOOD" if gap < 0.10 else "MISMATCH — investigate"
        print(f"    corrected mid={mid:.4f}  vs Gamma ask={gamma_best_ask}  ({verdict})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition", help="specific conditionId to inspect")
    args = parser.parse_args()

    if args.condition:
        title, market = _find_market_by_condition(args.condition)
    else:
        title, market = _find_first_map_market()

    if market is None:
        print("No live Valorant per-map markets found.")
        sys.exit(1)

    condition_id = market.get("conditionId")
    question = market.get("question", "")
    gamma_best_bid = float(market.get("bestBid", 0)) or None
    gamma_best_ask = float(market.get("bestAsk", 0)) or None

    print(f"Event:    {title}")
    print(f"Market:   {question}")
    print(f"condId:   {condition_id}")
    print(f"Gamma:    bestBid={gamma_best_bid}   bestAsk={gamma_best_ask}")

    trader = PolyTrader()
    info = trader.get_market_info(condition_id)
    token_a = info["token_a"]
    token_b = info["token_b"]

    _analyze("TOKEN A (outcome 0 — YES/first team)", token_a, trader,
             gamma_best_bid, gamma_best_ask)
    _analyze("TOKEN B (outcome 1 — NO/second team)", token_b, trader,
             # complement: token_b's ask ≈ 1 - token_a's bid
             round(1 - gamma_best_ask, 4) if gamma_best_ask else None,
             round(1 - gamma_best_bid, 4) if gamma_best_bid else None)

    print("\nDone.")


if __name__ == "__main__":
    main()
