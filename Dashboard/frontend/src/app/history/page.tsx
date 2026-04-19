"use client";

import { useEffect, useState } from "react";
import type {
  TradeSummary,
  ChartData,
  MatchHistory,
  MapHistory,
  SideHistory,
} from "@/lib/types";
import { api } from "@/lib/api";
import PriceChart from "@/components/charts/PriceChart";

const REASON_COLORS: Record<string, string> = {
  trailing_floor: "var(--accent)",
  stop_loss: "var(--negative)",
  resolution: "var(--warning)",
};

export default function HistoryPage() {
  const [matches, setMatches] = useState<MatchHistory[]>([]);
  const [summary, setSummary] = useState<TradeSummary | null>(null);
  const [expandedMatch, setExpandedMatch] = useState<string | null>(null);

  useEffect(() => {
    api.matches().then(setMatches);
    api.tradeSummary().then(setSummary);
  }, []);

  const toggleMatch = (matchId: string) => {
    setExpandedMatch((prev) => (prev === matchId ? null : matchId));
  };

  return (
    <div>
      {summary && (
        <div className="flex gap-4 mb-6">
          <SummaryCard
            label="Total PnL"
            value={`$${summary.total_pnl >= 0 ? "+" : ""}${summary.total_pnl.toFixed(2)}`}
            color={summary.total_pnl >= 0 ? "var(--positive)" : "var(--negative)"}
          />
          <SummaryCard
            label="Win Rate"
            value={`${(summary.win_rate * 100).toFixed(1)}%`}
          />
          <SummaryCard
            label="Avg Edge"
            value={`${(summary.avg_edge * 100).toFixed(1)}%`}
          />
          <SummaryCard
            label="Trades"
            value={summary.trade_count.toString()}
          />
        </div>
      )}

      {summary && Object.keys(summary.by_exit_reason).length > 0 && (
        <div className="flex gap-3 mb-6">
          {Object.entries(summary.by_exit_reason).map(([reason, stats]) => (
            <div
              key={reason}
              className="bg-[var(--bg-card)] border border-[var(--border)] rounded-lg px-4 py-3 text-xs"
            >
              <span
                className="font-semibold"
                style={{ color: REASON_COLORS[reason] ?? "var(--text-secondary)" }}
              >
                {reason.replace("_", " ")}
              </span>
              <span className="text-[var(--text-muted)] ml-2">
                {stats.count}x &middot; avg ${stats.avg_pnl >= 0 ? "+" : ""}{stats.avg_pnl.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl overflow-hidden">
        <div className="grid grid-cols-[120px_1fr_80px_120px_80px] gap-3 px-5 py-3 border-b border-[var(--border)] text-[var(--text-muted)] text-xs uppercase tracking-wider">
          <div>Date</div>
          <div>Match</div>
          <div className="text-center">Maps</div>
          <div className="text-right">PnL</div>
          <div className="text-right" />
        </div>

        {matches.map((m) => (
          <MatchRow
            key={m.match_id}
            match={m}
            expanded={expandedMatch === m.match_id}
            onToggle={() => toggleMatch(m.match_id)}
          />
        ))}

        {matches.length === 0 && (
          <div className="px-5 py-8 text-center text-[var(--text-muted)]">
            No evaluated matches yet
          </div>
        )}
      </div>
    </div>
  );
}

function MatchRow({
  match,
  expanded,
  onToggle,
}: {
  match: MatchHistory;
  expanded: boolean;
  onToggle: () => void;
}) {
  const date = new Date(match.evaluated_at);
  const dateStr = `${date.getMonth() + 1}/${date.getDate()}`;
  const timeStr = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const pnl = match.total_pnl;
  const hasBets = match.maps.some((mp) => mp.sides.some((s) => s.bet !== null));
  const pnlColor = !hasBets
    ? "var(--text-muted)"
    : pnl >= 0
      ? "var(--positive)"
      : "var(--negative)";

  return (
    <>
      <div
        className="grid grid-cols-[120px_1fr_80px_120px_80px] gap-3 px-5 py-3 border-b border-[var(--border)] hover:bg-[var(--bg-card-hover)] cursor-pointer transition-colors items-center text-sm"
        onClick={onToggle}
      >
        <div className="text-[var(--text-secondary)]">
          <div className="num">{dateStr}</div>
          <div className="text-xs text-[var(--text-muted)]">{timeStr}</div>
        </div>
        <div>
          {match.team_a} <span className="text-[var(--text-muted)]">vs</span> {match.team_b}
        </div>
        <div className="text-center num text-[var(--text-secondary)]">
          {match.maps.length}
        </div>
        <div className="text-right num font-semibold" style={{ color: pnlColor }}>
          {hasBets ? `$${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "—"}
        </div>
        <div className="text-right text-[var(--text-muted)]">
          {expanded ? "▾" : "▸"}
        </div>
      </div>

      {expanded && (
        <div className="px-5 py-4 bg-[var(--bg-primary)] border-b border-[var(--border)] space-y-4">
          {match.maps.map((mp) => (
            <MapCard key={mp.map_num} matchId={match.match_id} map={mp} />
          ))}
        </div>
      )}
    </>
  );
}

function MapCard({ matchId, map }: { matchId: string; map: MapHistory }) {
  const [activeTeam, setActiveTeam] = useState<string>(map.sides[0]?.team ?? "");

  const activeSide = map.sides.find((s) => s.team === activeTeam) ?? map.sides[0];

  return (
    <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-[var(--border)]">
        <div className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider">
          Map {map.map_num}
        </div>
        <div className="flex gap-1">
          {map.sides.map((s) => {
            const active = s.team === activeTeam;
            const hasBet = s.bet !== null;
            return (
              <button
                key={s.team}
                onClick={() => setActiveTeam(s.team)}
                className={`px-3 py-1 text-xs rounded transition-colors ${
                  active
                    ? "bg-[var(--accent)] text-[var(--bg-primary)] font-semibold"
                    : "bg-[var(--bg-primary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
                }`}
              >
                {s.team}
                {hasBet && (
                  <span className={`ml-1.5 inline-block w-1.5 h-1.5 rounded-full ${active ? "bg-[var(--bg-primary)]" : "bg-[var(--accent)]"}`} />
                )}
              </button>
            );
          })}
        </div>
      </div>

      {activeSide && (
        <SideView matchId={matchId} mapNum={map.map_num} side={activeSide} />
      )}
    </div>
  );
}

function SideView({
  matchId,
  mapNum,
  side,
}: {
  matchId: string;
  mapNum: number;
  side: SideHistory;
}) {
  const [chart, setChart] = useState<ChartData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    setChart(null);
    api
      .tradeChart(matchId, mapNum, side.team)
      .then((data) => {
        setChart(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [matchId, mapNum, side.team]);

  const bet = side.bet;
  const noEdge = side.no_edge;

  return (
    <div className="px-4 py-3">
      <div className="mb-3">
        {bet ? <BetSummary bet={bet} /> : <NoEdgeSummary info={noEdge} />}
      </div>

      {loading ? (
        <div className="h-[240px] flex items-center justify-center text-[var(--text-muted)] text-sm">
          Loading chart...
        </div>
      ) : chart && chart.ticks.length > 0 ? (
        <PriceChart
          ticks={chart.ticks}
          events={chart.events.filter((e) => e.side === side.team)}
          entryPrice={bet?.entry_price ?? undefined}
          exitPrice={bet?.exit_price ?? undefined}
          height={240}
        />
      ) : (
        <div className="h-[240px] flex items-center justify-center text-[var(--text-muted)] text-sm">
          No price data recorded for {side.team}
        </div>
      )}
    </div>
  );
}

function BetSummary({ bet }: { bet: NonNullable<SideHistory["bet"]> }) {
  const pnl = bet.pnl ?? 0;
  const pnlColor = pnl >= 0 ? "var(--positive)" : "var(--negative)";
  const reason = bet.exit_reason ?? "";

  return (
    <div className="grid grid-cols-6 gap-3 text-xs">
      <Stat label="Status" value={bet.status} />
      <Stat
        label="Edge"
        value={bet.edge !== null && bet.edge !== undefined ? `${(bet.edge * 100).toFixed(1)}%` : "—"}
      />
      <Stat
        label="Entry"
        value={bet.entry_price !== null && bet.entry_price !== undefined ? `$${bet.entry_price.toFixed(2)}` : "—"}
      />
      <Stat
        label="Exit"
        value={bet.exit_price !== null && bet.exit_price !== undefined ? `$${bet.exit_price.toFixed(2)}` : "—"}
      />
      <Stat
        label="PnL"
        value={bet.status === "closed" ? `$${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "—"}
        color={bet.status === "closed" ? pnlColor : undefined}
      />
      <Stat
        label="Reason"
        value={reason ? reason.replace("_", " ") : "—"}
        color={REASON_COLORS[reason]}
      />
    </div>
  );
}

function NoEdgeSummary({ info }: { info: SideHistory["no_edge"] }) {
  if (!info) {
    return (
      <div className="text-xs text-[var(--text-muted)]">N/A — not evaluated</div>
    );
  }
  return (
    <div className="grid grid-cols-6 gap-3 text-xs">
      <Stat label="Status" value="no edge" color="var(--text-muted)" />
      <Stat
        label="Model"
        value={info.model_prob !== null ? `${(info.model_prob * 100).toFixed(1)}%` : "—"}
      />
      <Stat
        label="Market"
        value={info.market_price !== null ? `$${info.market_price.toFixed(2)}` : "—"}
      />
      <Stat
        label="Edge"
        value={info.edge !== null ? `${(info.edge * 100).toFixed(1)}%` : "—"}
      />
      <Stat label="Entry" value="—" />
      <Stat label="PnL" value="—" />
    </div>
  );
}

function Stat({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div>
      <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-0.5">
        {label}
      </div>
      <div className="num text-[var(--text-primary)]" style={color ? { color } : undefined}>
        {value}
      </div>
    </div>
  );
}

function SummaryCard({
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
