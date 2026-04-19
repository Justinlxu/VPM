export interface Position {
  key: string;
  match_id: string;
  map_num: number;
  team_a: string;
  team_b: string;
  bet_team: string;
  entry_price: number;
  high_price: number;
  shares: number;
  position_usd: number;
  current_price: number | null;
  current_pnl: number | null;
  unrealized_pnl_pct: number | null;
  trailing_floor: number | null;
  hold_to_resolution: boolean;
}

export interface DashboardData {
  mode: string;
  bankroll: number;
  positions: Position[];
  total_unrealized_pnl: number;
  active_count: number;
  saved_at: string | null;
}

export interface PriceTick {
  timestamp: string;
  price: number;
  side: string;
}

export interface TradeEvent {
  timestamp: string;
  event_type: string;
  side: string;
  price: number;
  reason: string | null;
}

export interface ChartData {
  ticks: PriceTick[];
  events: TradeEvent[];
}

export interface Trade {
  match_id: string;
  map_num: number;
  team_a: string;
  team_b: string;
  side: string;
  model_prob: number | null;
  market_price: number | null;
  edge: number | null;
  kelly_pct: number | null;
  position_size_usd: number | null;
  entry_price: number | null;
  entry_timestamp: string;
  exit_price: number | null;
  exit_reason: string;
  pnl: number | null;
  exit_timestamp: string;
  status: string;
}

export interface ExitReasonStats {
  count: number;
  total_pnl: number;
  avg_pnl: number;
}

export interface BetDetails {
  side: string;
  status: "open" | "closed";
  model_prob?: number | null;
  market_price?: number | null;
  edge?: number | null;
  kelly_pct?: number | null;
  position_size_usd?: number | null;
  entry_price?: number | null;
  entry_timestamp?: string;
  exit_price?: number | null;
  exit_reason?: string;
  pnl?: number | null;
  exit_timestamp?: string;
}

export interface NoEdgeDetails {
  model_prob: number | null;
  market_price: number | null;
  edge: number | null;
}

export interface SideHistory {
  team: string;
  bet: BetDetails | null;
  no_edge: NoEdgeDetails | null;
}

export interface MapHistory {
  map_num: number;
  team_a: string;
  team_b: string;
  sides: SideHistory[];
}

export interface MatchHistory {
  match_id: string;
  team_a: string;
  team_b: string;
  evaluated_at: string;
  total_pnl: number;
  maps: MapHistory[];
}

export interface TradeSummary {
  total_pnl: number;
  trade_count: number;
  win_rate: number;
  avg_edge: number;
  by_exit_reason: Record<string, ExitReasonStats>;
  open_count: number;
  no_edge_count: number;
}

export interface PlayerElo {
  player: string;
  team: string;
  elo: number;
}

export interface TeamElo {
  team: string;
  elo: number;
  players: { player: string; elo: number }[];
}

export interface EloPoint {
  timestamp: string;
  elo: number;
}

export interface PredictResult {
  prob_a: number;
  prob_b: number;
  team_a: string;
  team_b: string;
  elo_features: Record<string, number>;
  players: Record<string, { player: string; elo: number; fkfd: number }[]>;
  maps_played: Record<string, number>;
}
