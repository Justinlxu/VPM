"""Trade data from SQLite trades table."""

from Dashboard.backend.services.db import get_db


def _fetch_all_trades(mode="live"):
    """Return all trade rows for a given mode."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE mode = ? ORDER BY timestamp",
            (mode,),
        ).fetchall()
    return [dict(r) for r in rows]


def _merge_trades(rows):
    """Merge open/close row pairs into single trade objects.

    Returns (closed_trades, open_trades, no_edge_trades).
    """
    groups = {}
    no_edge = []

    for row in rows:
        reason = row.get("exit_reason", "")
        if reason == "no_edge":
            no_edge.append({
                "timestamp": row["timestamp"],
                "match_id": row["match_id"],
                "map_num": row["map_num"],
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "side": row["side"],
                "model_prob": row.get("model_prob"),
                "market_price": row.get("market_price"),
                "edge": row.get("edge"),
                "exit_reason": "no_edge",
            })
            continue
        if reason == "below_min":
            continue

        key = (row["match_id"], row["map_num"], row["side"])
        if key not in groups:
            groups[key] = {"open": None, "close": None}

        if reason == "open":
            groups[key]["open"] = row
        elif row.get("exit_price") is not None:
            groups[key]["close"] = row

    closed = []
    still_open = []

    for key, pair in groups.items():
        entry = pair["open"]
        exit_row = pair["close"]

        if not entry:
            continue

        trade = {
            "match_id": entry["match_id"],
            "map_num": entry["map_num"],
            "team_a": entry["team_a"],
            "team_b": entry["team_b"],
            "side": entry["side"],
            "model_prob": entry.get("model_prob"),
            "market_price": entry.get("market_price"),
            "edge": entry.get("edge"),
            "kelly_pct": entry.get("kelly_pct"),
            "position_size_usd": entry.get("position_size_usd"),
            "entry_price": entry.get("entry_price"),
            "entry_timestamp": entry["timestamp"],
        }

        if exit_row:
            trade.update({
                "exit_price": exit_row.get("exit_price"),
                "exit_reason": exit_row.get("exit_reason", ""),
                "pnl": exit_row.get("pnl"),
                "exit_timestamp": exit_row["timestamp"],
                "status": "closed",
            })
            closed.append(trade)
        else:
            trade["status"] = "open"
            still_open.append(trade)

    return closed, still_open, no_edge


def get_trades(mode="live", team=None, exit_reason=None, date_from=None, date_to=None):
    """Return merged closed trades with optional filters."""
    rows = _fetch_all_trades(mode)
    closed, _, _ = _merge_trades(rows)

    if team:
        team_lower = team.lower()
        closed = [t for t in closed if team_lower in t["side"].lower()]
    if exit_reason:
        closed = [t for t in closed if t["exit_reason"] == exit_reason]
    if date_from:
        closed = [t for t in closed if t["entry_timestamp"] >= date_from]
    if date_to:
        closed = [t for t in closed if t["entry_timestamp"] <= date_to]

    closed.sort(key=lambda t: t["entry_timestamp"], reverse=True)
    return closed


def get_matches(mode="live"):
    """Group evaluated maps into a match→maps→sides tree.

    Every map the agent evaluates writes at least a row to `trades`
    (no_edge rows when neither side cleared the threshold, open/close
    rows when a bet was placed). Grouping these lets the history view
    show every evaluated match — not only the ones we traded.
    """
    rows = _fetch_all_trades(mode)

    matches = {}

    for row in rows:
        match_id = row["match_id"]
        map_num = row["map_num"]
        reason = row.get("exit_reason", "")

        if reason == "below_min":
            continue

        if match_id not in matches:
            matches[match_id] = {
                "match_id": match_id,
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "evaluated_at": row["timestamp"],
                "maps": {},
                "total_pnl": 0.0,
            }
        m = matches[match_id]
        if row["timestamp"] < m["evaluated_at"]:
            m["evaluated_at"] = row["timestamp"]

        if map_num not in m["maps"]:
            m["maps"][map_num] = {
                "map_num": map_num,
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "sides": {},
            }
        map_entry = m["maps"][map_num]

        for team in (row["team_a"], row["team_b"]):
            if team not in map_entry["sides"]:
                map_entry["sides"][team] = {
                    "team": team,
                    "bet": None,
                    "no_edge": None,
                }

        side = row["side"]

        if reason == "no_edge":
            # Agent logs team_a's prob/market/edge for no_edge rows.
            # team_b's view is the complement: prob_b = 1 - prob_a,
            # market_b ≈ 1 - market_a (binary complementary tokens).
            p_a = row.get("model_prob")
            mk_a = row.get("market_price")
            e_a = row.get("edge")
            map_entry["sides"][row["team_a"]]["no_edge"] = {
                "model_prob": p_a,
                "market_price": mk_a,
                "edge": e_a,
            }
            map_entry["sides"][row["team_b"]]["no_edge"] = {
                "model_prob": (1 - p_a) if p_a is not None else None,
                "market_price": (1 - mk_a) if mk_a is not None else None,
                "edge": (-e_a) if e_a is not None else None,
            }
            continue

        if side not in map_entry["sides"]:
            map_entry["sides"][side] = {"team": side, "bet": None, "no_edge": None}
        side_entry = map_entry["sides"][side]

        if reason == "open":
            side_entry["bet"] = side_entry["bet"] or {}
            side_entry["bet"].update({
                "side": side,
                "status": "open",
                "model_prob": row.get("model_prob"),
                "market_price": row.get("market_price"),
                "edge": row.get("edge"),
                "kelly_pct": row.get("kelly_pct"),
                "position_size_usd": row.get("position_size_usd"),
                "entry_price": row.get("entry_price"),
                "entry_timestamp": row["timestamp"],
            })
        elif row.get("exit_price") is not None:
            side_entry["bet"] = side_entry["bet"] or {}
            side_entry["bet"].update({
                "side": side,
                "status": "closed",
                "exit_price": row.get("exit_price"),
                "exit_reason": reason,
                "pnl": row.get("pnl"),
                "exit_timestamp": row["timestamp"],
            })
            if row.get("pnl") is not None:
                m["total_pnl"] += row["pnl"]

    out = []
    for m in matches.values():
        maps_list = []
        for map_entry in m["maps"].values():
            # If one side has a bet but the other has no no_edge, fill in the
            # other side's implied model/market as complements so the tab still
            # shows per-team numbers instead of "N/A — not evaluated".
            sides_dict = map_entry["sides"]
            for team, s in sides_dict.items():
                if s["bet"] is not None or s["no_edge"] is not None:
                    continue
                other = next((o for o in sides_dict.values() if o["team"] != team), None)
                if other and other["bet"] is not None:
                    bet = other["bet"]
                    p = bet.get("model_prob")
                    mk = bet.get("market_price")
                    e = bet.get("edge")
                    s["no_edge"] = {
                        "model_prob": (1 - p) if p is not None else None,
                        "market_price": (1 - mk) if mk is not None else None,
                        "edge": (-e) if e is not None else None,
                    }

            sides_list = sorted(
                sides_dict.values(),
                key=lambda s: s["team"],
            )
            maps_list.append({
                "map_num": map_entry["map_num"],
                "team_a": map_entry["team_a"],
                "team_b": map_entry["team_b"],
                "sides": sides_list,
            })
        maps_list.sort(key=lambda x: x["map_num"])
        out.append({
            "match_id": m["match_id"],
            "team_a": m["team_a"],
            "team_b": m["team_b"],
            "evaluated_at": m["evaluated_at"],
            "total_pnl": round(m["total_pnl"], 2),
            "maps": maps_list,
        })

    out.sort(key=lambda x: x["evaluated_at"], reverse=True)
    return out


def get_trade_summary(mode="live"):
    """Compute aggregate trade stats."""
    rows = _fetch_all_trades(mode)
    closed, open_trades, no_edge = _merge_trades(rows)

    if not closed:
        return {
            "total_pnl": 0, "trade_count": 0, "win_rate": 0,
            "avg_edge": 0, "by_exit_reason": {}, "open_count": len(open_trades),
        }

    pnls = [t["pnl"] for t in closed if t["pnl"] is not None]
    edges = [t["edge"] for t in closed if t["edge"] is not None]
    wins = sum(1 for p in pnls if p > 0)

    by_reason = {}
    for t in closed:
        reason = t.get("exit_reason", "unknown")
        if reason not in by_reason:
            by_reason[reason] = {"count": 0, "total_pnl": 0.0}
        by_reason[reason]["count"] += 1
        if t["pnl"] is not None:
            by_reason[reason]["total_pnl"] += t["pnl"]

    for data in by_reason.values():
        data["avg_pnl"] = round(data["total_pnl"] / data["count"], 2) if data["count"] else 0
        data["total_pnl"] = round(data["total_pnl"], 2)

    return {
        "total_pnl": round(sum(pnls), 2),
        "trade_count": len(closed),
        "win_rate": round(wins / len(pnls), 4) if pnls else 0,
        "avg_edge": round(sum(edges) / len(edges), 4) if edges else 0,
        "by_exit_reason": by_reason,
        "open_count": len(open_trades),
        "no_edge_count": len(no_edge),
    }
