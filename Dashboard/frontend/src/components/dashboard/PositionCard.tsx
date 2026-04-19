"use client";

import { useEffect, useState } from "react";
import type { Position, ChartData } from "@/lib/types";
import { api } from "@/lib/api";
import PriceChart from "@/components/charts/PriceChart";

export default function PositionCard({ pos }: { pos: Position }) {
  const [chart, setChart] = useState<ChartData | null>(null);

  useEffect(() => {
    api.positionChart(pos.match_id, pos.map_num, pos.bet_team).then(setChart);
    const interval = setInterval(() => {
      api.positionChart(pos.match_id, pos.map_num, pos.bet_team).then(setChart);
    }, 5000);
    return () => clearInterval(interval);
  }, [pos.match_id, pos.map_num, pos.bet_team]);

  const pnlColor = (pos.current_pnl ?? 0) >= 0 ? "var(--positive)" : "var(--negative)";
  const pnlPct = pos.unrealized_pnl_pct !== null ? (pos.unrealized_pnl_pct * 100).toFixed(1) : "--";

  return (
    <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl overflow-hidden">
      {/* Header */}
      <div className="px-5 py-4 border-b border-[var(--border)] flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">
            {pos.team_a} vs {pos.team_b}
          </div>
          <div className="text-xs text-[var(--text-muted)] mt-0.5">
            Map {pos.map_num} &middot; {pos.bet_team}
          </div>
        </div>
        <div className="text-right">
          <div className="num text-sm font-semibold" style={{ color: pnlColor }}>
            ${pos.current_pnl !== null ? pos.current_pnl.toFixed(2) : "--"}
          </div>
          <div className="num text-xs" style={{ color: pnlColor }}>
            {(pos.current_pnl ?? 0) >= 0 ? "+" : ""}{pnlPct}%
          </div>
        </div>
      </div>

      {/* Chart */}
      <div className="px-2 py-2">
        {chart && chart.ticks.length > 0 ? (
          <PriceChart
            ticks={chart.ticks}
            events={chart.events}
            entryPrice={pos.entry_price}
            floorPrice={pos.trailing_floor}
            height={250}
          />
        ) : (
          <div className="h-[250px] flex items-center justify-center text-[var(--text-muted)] text-sm">
            Loading chart...
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-5 py-3 border-t border-[var(--border)] flex gap-6 text-xs text-[var(--text-secondary)]">
        <div>
          <span className="text-[var(--text-muted)]">Entry </span>
          <span className="num">${pos.entry_price.toFixed(2)}</span>
        </div>
        <div>
          <span className="text-[var(--text-muted)]">Current </span>
          <span className="num">${pos.current_price?.toFixed(2) ?? "--"}</span>
        </div>
        <div>
          <span className="text-[var(--text-muted)]">High </span>
          <span className="num">${pos.high_price.toFixed(2)}</span>
        </div>
        <div>
          <span className="text-[var(--text-muted)]">Shares </span>
          <span className="num">{pos.shares.toFixed(1)}</span>
        </div>
        <div>
          <span className="text-[var(--text-muted)]">Floor </span>
          <span className="num">{pos.trailing_floor ? `$${pos.trailing_floor.toFixed(2)}` : "inactive"}</span>
        </div>
      </div>
    </div>
  );
}
