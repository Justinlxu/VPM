"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const tabs = [
  { href: "/dashboard", label: "Dashboard", icon: "◉" },
  { href: "/history", label: "History", icon: "◷" },
  { href: "/leaderboard", label: "Leaderboard", icon: "△" },
  { href: "/predict", label: "Predict", icon: "⚡" },
];

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <nav className="fixed left-0 top-0 h-full w-[200px] bg-[var(--bg-card)] border-r border-[var(--border)] flex flex-col z-50">
      <div className="px-5 py-5 border-b border-[var(--border)]">
        <h1 className="text-lg font-bold tracking-wide text-[var(--accent)]">
          VPM
        </h1>
        <p className="text-xs text-[var(--text-muted)] mt-0.5">Trading Dashboard</p>
      </div>

      <div className="flex flex-col gap-1 mt-4 px-3">
        {tabs.map((tab) => {
          const active = pathname === tab.href || pathname.startsWith(tab.href + "/");
          return (
            <Link
              key={tab.href}
              href={tab.href}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors ${
                active
                  ? "bg-[var(--accent)]/10 text-[var(--accent)] border-l-2 border-[var(--accent)]"
                  : "text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-card-hover)]"
              }`}
            >
              <span className="text-base">{tab.icon}</span>
              {tab.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
