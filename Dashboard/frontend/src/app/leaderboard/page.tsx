"use client";

import { useEffect, useState } from "react";
import type { PlayerElo, TeamElo } from "@/lib/types";
import { api } from "@/lib/api";

export default function LeaderboardPage() {
  const [players, setPlayers] = useState<PlayerElo[]>([]);
  const [teams, setTeams] = useState<TeamElo[]>([]);

  useEffect(() => {
    api.eloPlayers().then(setPlayers);
    api.eloTeams().then(setTeams);
  }, []);

  const topPlayers = players.slice(0, 10);
  const bottomPlayers = [...players].reverse().slice(0, 10);
  const topTeams = teams.slice(0, 10);

  return (
    <div>
      {/* Teams */}
      <h2 className="text-lg font-semibold mb-4">Team Rankings</h2>
      <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl overflow-hidden mb-8">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-[var(--text-muted)] text-xs uppercase tracking-wider">
              <th className="text-left px-5 py-3 w-12">#</th>
              <th className="text-left px-3 py-3">Team</th>
              <th className="text-right px-3 py-3">Team Elo</th>
              <th className="text-left px-5 py-3">Players</th>
            </tr>
          </thead>
          <tbody>
            {topTeams.map((team, i) => (
              <tr
                key={team.team}
                className="border-b border-[var(--border)] hover:bg-[var(--bg-card-hover)] transition-colors"
              >
                <td className="px-5 py-3 text-[var(--text-muted)] num">{i + 1}</td>
                <td className="px-3 py-3 font-semibold">{team.team}</td>
                <td className="px-3 py-3 text-right num text-[var(--accent)]">
                  {team.elo.toFixed(0)}
                </td>
                <td className="px-5 py-3">
                  <div className="flex gap-4 text-xs text-[var(--text-secondary)]">
                    {team.players
                      .sort((a, b) => b.elo - a.elo)
                      .map((p) => (
                        <span key={p.player}>
                          {p.player}{" "}
                          <span className="num text-[var(--text-muted)]">
                            {p.elo.toFixed(0)}
                          </span>
                        </span>
                      ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {teams.length === 0 && (
          <div className="px-5 py-8 text-center text-[var(--text-muted)]">
            Not enough data yet &mdash; Elo snapshots populate as the agent evaluates matches
          </div>
        )}
      </div>

      {/* Players: Top & Bottom side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <h2 className="text-lg font-semibold mb-4">Top Players</h2>
          <PlayerTable players={topPlayers} startRank={1} />
        </div>
        <div>
          <h2 className="text-lg font-semibold mb-4">Bottom Players</h2>
          <PlayerTable players={bottomPlayers} startRank={players.length - 9} ascending />
        </div>
      </div>
    </div>
  );
}

function PlayerTable({
  players,
  startRank,
  ascending,
}: {
  players: PlayerElo[];
  startRank: number;
  ascending?: boolean;
}) {
  return (
    <div className="bg-[var(--bg-card)] border border-[var(--border)] rounded-xl overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-[var(--border)] text-[var(--text-muted)] text-xs uppercase tracking-wider">
            <th className="text-left px-5 py-3 w-12">#</th>
            <th className="text-left px-3 py-3">Player</th>
            <th className="text-left px-3 py-3">Team</th>
            <th className="text-right px-5 py-3">Elo</th>
          </tr>
        </thead>
        <tbody>
          {players.map((p, i) => {
            const rank = ascending ? startRank + (players.length - 1 - i) : startRank + i;
            return (
              <tr
                key={p.player}
                className="border-b border-[var(--border)] hover:bg-[var(--bg-card-hover)] transition-colors"
              >
                <td className="px-5 py-3 text-[var(--text-muted)] num">{rank}</td>
                <td className="px-3 py-3 font-semibold">{p.player}</td>
                <td className="px-3 py-3 text-[var(--text-secondary)]">{p.team}</td>
                <td className="px-5 py-3 text-right num text-[var(--accent)]">
                  {p.elo.toFixed(0)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {players.length === 0 && (
        <div className="px-5 py-8 text-center text-[var(--text-muted)]">
          No data yet
        </div>
      )}
    </div>
  );
}
