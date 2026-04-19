"use client";

import { useState } from "react";
import type { PredictResult } from "@/lib/types";
import { api } from "@/lib/api";

const MAPS = ["", "Ascent", "Bind", "Haven", "Split", "Icebox", "Lotus", "Sunset", "Abyss", "Pearl", "Fracture", "Breeze"];

export default function PredictPage() {
  const [teamA, setTeamA] = useState("");
  const [teamB, setTeamB] = useState("");
  const [mapName, setMapName] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<PredictResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handlePredict = async () => {
    if (!teamA.trim() || !teamB.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.predict(teamA.trim(), teamB.trim(), mapName || undefined);
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Prediction failed");
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-3xl">
      {/* Input form */}
      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-6 mb-6">
        <div className="grid grid-cols-2 gap-4 mb-4">
          <div>
            <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider block mb-2">
              Team A
            </label>
            <input
              type="text"
              value={teamA}
              onChange={(e) => setTeamA(e.target.value)}
              placeholder="e.g. Fnatic"
              className="w-full bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg px-4 py-2.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] outline-none transition-colors"
              onKeyDown={(e) => e.key === "Enter" && handlePredict()}
            />
          </div>
          <div>
            <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider block mb-2">
              Team B
            </label>
            <input
              type="text"
              value={teamB}
              onChange={(e) => setTeamB(e.target.value)}
              placeholder="e.g. Team Liquid"
              className="w-full bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg px-4 py-2.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] outline-none transition-colors"
              onKeyDown={(e) => e.key === "Enter" && handlePredict()}
            />
          </div>
        </div>
        <div className="flex gap-4 items-end">
          <div className="flex-1">
            <label className="text-xs text-[var(--text-muted)] uppercase tracking-wider block mb-2">
              Map (optional)
            </label>
            <select
              value={mapName}
              onChange={(e) => setMapName(e.target.value)}
              className="w-full bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg px-4 py-2.5 text-sm text-[var(--text-primary)] outline-none"
            >
              <option value="">Any map</option>
              {MAPS.filter(Boolean).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>
          <button
            onClick={handlePredict}
            disabled={loading || !teamA.trim() || !teamB.trim()}
            className="px-6 py-2.5 bg-[var(--accent)] text-[var(--bg-primary)] rounded-lg text-sm font-semibold hover:opacity-90 disabled:opacity-40 transition-opacity"
          >
            {loading ? "Running model..." : "Predict"}
          </button>
        </div>
        {error && (
          <div className="mt-3 text-sm text-[var(--negative)]">{error}</div>
        )}
      </div>

      {/* Results */}
      {result && (
        <div>
          {/* Probability bar */}
          <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl p-6 mb-6">
            <div className="flex justify-between mb-3">
              <div>
                <div className="text-sm font-semibold">{result.team_a}</div>
                <div className="num text-2xl font-bold text-[var(--accent)]">
                  {(result.prob_a * 100).toFixed(1)}%
                </div>
              </div>
              <div className="text-right">
                <div className="text-sm font-semibold">{result.team_b}</div>
                <div className="num text-2xl font-bold text-[var(--text-secondary)]">
                  {(result.prob_b * 100).toFixed(1)}%
                </div>
              </div>
            </div>
            {/* Visual bar */}
            <div className="h-3 rounded-full overflow-hidden flex bg-[var(--bg-primary)]">
              <div
                className="h-full rounded-l-full transition-all"
                style={{
                  width: `${result.prob_a * 100}%`,
                  background: "var(--accent)",
                }}
              />
              <div
                className="h-full rounded-r-full transition-all"
                style={{
                  width: `${result.prob_b * 100}%`,
                  background: "var(--text-muted)",
                }}
              />
            </div>
            {/* Elo summary */}
            <div className="flex justify-between mt-4 text-xs text-[var(--text-secondary)]">
              <div>
                Elo: <span className="num">{result.elo_features.ranking_elo_sum_a?.toFixed(0)}</span>
                <span className="text-[var(--text-muted)] ml-2">
                  trend {result.elo_features.ranking_elo_trend_a >= 0 ? "+" : ""}
                  {result.elo_features.ranking_elo_trend_a?.toFixed(0)}
                </span>
              </div>
              <div>
                <span className="text-[var(--text-muted)] mr-2">
                  trend {result.elo_features.ranking_elo_trend_b >= 0 ? "+" : ""}
                  {result.elo_features.ranking_elo_trend_b?.toFixed(0)}
                </span>
                Elo: <span className="num">{result.elo_features.ranking_elo_sum_b?.toFixed(0)}</span>
              </div>
            </div>
          </div>

          {/* Player rosters */}
          <div className="grid grid-cols-2 gap-4">
            {Object.entries(result.players).map(([team, players]) => (
              <div
                key={team}
                className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl overflow-hidden"
              >
                <div className="px-5 py-3 border-b border-[var(--border)] text-sm font-semibold">
                  {team}
                  {result.maps_played[team] && (
                    <span className="text-[var(--text-muted)] font-normal ml-2">
                      {result.maps_played[team]} maps
                    </span>
                  )}
                </div>
                <table className="w-full text-sm">
                  <tbody>
                    {players.map((p) => (
                      <tr
                        key={p.player}
                        className="border-b border-[var(--border)] last:border-0"
                      >
                        <td className="px-5 py-2">{p.player}</td>
                        <td className="px-3 py-2 text-right num text-[var(--accent)]">
                          {p.elo.toFixed(0)}
                        </td>
                        <td className="px-5 py-2 text-right num text-xs" style={{
                          color: p.fkfd >= 0 ? "var(--positive)" : "var(--negative)",
                        }}>
                          {p.fkfd >= 0 ? "+" : ""}{p.fkfd.toFixed(1)} FK/FD
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
