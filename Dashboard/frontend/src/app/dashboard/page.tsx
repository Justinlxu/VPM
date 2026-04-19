"use client";

import { useEffect, useState } from "react";
import type { DashboardData } from "@/lib/types";
import { api } from "@/lib/api";
import PositionCard from "@/components/dashboard/PositionCard";

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);

  useEffect(() => {
    const load = () => api.dashboard().then(setData).catch(console.error);
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  if (!data) {
    return (
      <div className="flex items-center justify-center h-[60vh] text-[var(--text-muted)]">
        Connecting to agent...
      </div>
    );
  }

  const pnlColor = data.total_unrealized_pnl >= 0 ? "var(--positive)" : "var(--negative)";

  return (
    <div>
      {/* Top bar */}
      <div className="flex gap-4 mb-6">
        <StatCard
          label="USDC Balance"
          value={`$${data.bankroll.toFixed(2)}`}
        />
        <StatCard
          label="Unrealized PnL"
          value={`$${data.total_unrealized_pnl >= 0 ? "+" : ""}${data.total_unrealized_pnl.toFixed(2)}`}
          color={pnlColor}
        />
        <StatCard
          label="Active Positions"
          value={data.active_count.toString()}
        />
        <StatCard
          label="Mode"
          value={data.mode.toUpperCase()}
          color={data.mode === "live" ? "var(--positive)" : "var(--warning)"}
        />
      </div>

      {/* Positions */}
      {data.positions.length === 0 ? (
        <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-12 text-center text-[var(--text-muted)]">
          No active positions &mdash; the agent is waiting for the next match
        </div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {data.positions.map((pos) => (
            <PositionCard key={pos.key} pos={pos} />
          ))}
        </div>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl px-5 py-4 flex-1">
      <div className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1">
        {label}
      </div>
      <div
        className="num text-xl font-semibold"
        style={{ color: color ?? "var(--text-primary)" }}
      >
        {value}
      </div>
    </div>
  );
}
