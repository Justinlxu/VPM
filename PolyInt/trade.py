"""
Polymarket Trade Execution Layer

Low-level wrapper around the Polymarket CLOB API.
Handles order placement, cancellation, position/balance queries, and price lookups.
No trading logic — the agent calls these functions.

Usage:
    from trade import PolyTrader
    trader = PolyTrader()
    trader.buy(token_id, price=0.55, size=100)
"""

import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, OrderArgs, OrderType,
    PartialCreateOrderOptions, BookParams,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


class PolyTrader:
    def __init__(self):
        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        creds = ApiCreds(
            api_key=os.getenv("POLY_API_KEY", ""),
            api_secret=os.getenv("POLY_API_SECRET", ""),
            api_passphrase=os.getenv("POLY_API_PASSPHRASE", ""),
        )

        funder = os.getenv("POLY_FUNDER", "")
        self.client = ClobClient(
            HOST, key=private_key, chain_id=CHAIN_ID, creds=creds,
            signature_type=2, funder=funder or None,
        )

        # Cache: condition_id -> {outcome_index: token_id, tick_size, neg_risk}
        self._market_cache = {}

    # ══════════════════════════════════════════════════════════════
    # MARKET LOOKUP
    # ══════════════════════════════════════════════════════════════

    def get_market_info(self, condition_id):
        """
        Look up token IDs and market config for a condition_id.

        Returns
        -------
        dict:
            {
                "token_a": str,   # token_id for outcome 0 (first team / Yes)
                "token_b": str,   # token_id for outcome 1 (second team / No)
                "tick_size": str,
                "neg_risk": bool,
            }
        """
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        market = self.client.get_market(condition_id)

        tokens = market.get("tokens", [])
        if len(tokens) < 2:
            raise ValueError(f"Market {condition_id} has fewer than 2 tokens")

        # tokens[0] = outcome 0 (typically first team listed), tokens[1] = outcome 1
        info = {
            "token_a": tokens[0]["token_id"],
            "token_b": tokens[1]["token_id"],
            "tick_size": str(market.get("minimum_tick_size", "0.01")),
            "neg_risk": market.get("neg_risk", False),
        }
        self._market_cache[condition_id] = info
        return info

    # ══════════════════════════════════════════════════════════════
    # ORDER PLACEMENT
    # ══════════════════════════════════════════════════════════════

    def buy(self, token_id, price, size, tick_size="0.01", neg_risk=False):
        """
        Place a limit buy order.

        Parameters
        ----------
        token_id : str
            The conditional token to buy.
        price : float
            Limit price (0 to 1).
        size : float
            Number of shares to buy.
        tick_size : str
            Price precision for this market.
        neg_risk : bool
            Whether this is a neg-risk market.

        Returns
        -------
        dict with order_id and status from the API.
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        signed = self.client.create_order(order_args, options)
        return self.client.post_order(signed, orderType=OrderType.GTC)

    def sell(self, token_id, price, size, tick_size="0.01", neg_risk=False):
        """
        Place a limit sell order.

        Parameters
        ----------
        token_id : str
            The conditional token to sell.
        price : float
            Limit price (0 to 1).
        size : float
            Number of shares to sell.
        tick_size : str
            Price precision for this market.
        neg_risk : bool
            Whether this is a neg-risk market.

        Returns
        -------
        dict with order_id and status from the API.
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="SELL",
        )
        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        signed = self.client.create_order(order_args, options)
        return self.client.post_order(signed, orderType=OrderType.GTC)

    # ══════════════════════════════════════════════════════════════
    # ORDER MANAGEMENT
    # ══════════════════════════════════════════════════════════════

    def get_order(self, order_id):
        """Get a single order's status and fill info."""
        return self.client.get_order(order_id)

    def cancel(self, order_id):
        """Cancel a single order by ID."""
        return self.client.cancel(order_id)

    def cancel_all(self):
        """Cancel all open orders."""
        return self.client.cancel_all()

    def get_open_orders(self):
        """Return list of all open orders."""
        return self.client.get_orders()

    def get_trades_for_order(self, order_id):
        """
        Get trades associated with a specific order.
        Returns list of trade dicts with 'price' and 'size' fields.
        """
        from py_clob_client.clob_types import TradeParams
        trades = self.client.get_trades(TradeParams(id=order_id))
        return trades

    # ══════════════════════════════════════════════════════════════
    # POSITIONS & BALANCE
    # ══════════════════════════════════════════════════════════════

    def get_balance(self):
        """
        Get USDC (collateral) balance available for trading.

        Returns
        -------
        float : USDC balance
        """
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
        )
        resp = self.client.get_balance_allowance(params)
        return float(resp.get("balance", 0)) / 1e6

    def get_position(self, token_id):
        """
        Get the number of shares held for a specific token.

        Returns
        -------
        float : number of shares held (in normal units, not raw)
        """
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
        )
        resp = self.client.get_balance_allowance(params)
        return float(resp.get("balance", 0)) / 1e6

    def approve_token(self, token_id):
        """
        Approve conditional token for trading (required before selling).
        Must be called after buying tokens to enable sells.
        """
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
        )
        return self.client.update_balance_allowance(params)

    # ══════════════════════════════════════════════════════════════
    # PRICE QUERIES
    # ══════════════════════════════════════════════════════════════

    def get_market_price(self, token_id):
        """
        Get the current best bid, best ask, and last trade price.

        Returns
        -------
        dict:
            {
                "best_bid": float or None,
                "best_ask": float or None,
                "last_trade": float or None,
                "mid": float or None,
            }
        """
        book = self.client.get_order_book(token_id)

        best_bid = None
        best_ask = None

        if book.bids:
            best_bid = max(float(b.price) for b in book.bids)
        if book.asks:
            best_ask = min(float(a.price) for a in book.asks)

        mid = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2

        last_trade = None
        if book.last_trade_price:
            try:
                last_trade = float(book.last_trade_price)
            except (ValueError, TypeError):
                pass

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "last_trade": last_trade,
            "mid": mid,
        }

    def get_last_trade(self, token_id):
        """
        Get the last trade price for a specific token via the dedicated endpoint.

        Unlike get_order_book().last_trade_price (which is market-level and
        returns the same value for both tokens), this endpoint is per-token.

        Returns
        -------
        float or None
        """
        try:
            resp = self.client.get_last_trade_price(token_id)
            return float(resp.get("price", 0)) or None
        except Exception:
            return None

    def get_midpoint(self, token_id):
        """Get the mid-market price for a token. Returns float or None."""
        try:
            resp = self.client.get_midpoint(token_id)
            return float(resp.get("mid", 0))
        except Exception:
            return None


# ══════════════════════════════════════════════════════════════
# CLI — quick test
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    trader = PolyTrader()

    print("Connection test...")
    print(f"  Health: {trader.client.get_ok()}")

    balance = trader.get_balance()
    print(f"  USDC balance: ${balance:.2f}")

    orders = trader.get_open_orders()
    print(f"  Open orders: {len(orders)}")
