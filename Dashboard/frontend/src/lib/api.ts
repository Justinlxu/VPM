const API = "http://127.0.0.1:8000";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

import type {
  DashboardData,
  ChartData,
  Trade,
  TradeSummary,
  MatchHistory,
  PlayerElo,
  TeamElo,
  EloPoint,
  PredictResult,
} from "./types";

export const api = {
  dashboard: () => get<DashboardData>("/api/dashboard"),
  positionChart: (matchId: string, mapNum: number, side?: string) => {
    const qs = side ? `?side=${encodeURIComponent(side)}` : "";
    return get<ChartData>(`/api/dashboard/prices/${matchId}/${mapNum}${qs}`);
  },

  trades: (params?: Record<string, string>) => {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get<Trade[]>(`/api/trades${qs}`);
  },
  tradeSummary: () => get<TradeSummary>("/api/trades/summary"),
  matches: () => get<MatchHistory[]>("/api/trades/matches"),
  tradeChart: (matchId: string, mapNum: number, side?: string) => {
    const qs = side ? `?side=${encodeURIComponent(side)}` : "";
    return get<ChartData>(`/api/trades/${matchId}/${mapNum}/chart${qs}`);
  },

  eloPlayers: () => get<PlayerElo[]>("/api/elo/players"),
  eloTeams: () => get<TeamElo[]>("/api/elo/teams"),
  eloHistory: (params: { player?: string; team?: string }) => {
    const qs = new URLSearchParams(params as Record<string, string>).toString();
    return get<EloPoint[]>(`/api/elo/history?${qs}`);
  },

  predict: (teamA: string, teamB: string, mapName?: string) =>
    post<PredictResult>("/api/predict", {
      team_a: teamA,
      team_b: teamB,
      map_name: mapName || null,
    }),
};

export const WS_URL = "ws://127.0.0.1:8000/ws/prices";
