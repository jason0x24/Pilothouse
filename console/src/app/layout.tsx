import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";

import { TenantBadge } from "@/components/TenantBadge";

export const metadata: Metadata = {
  title: "Pilothouse",
  description: "AI DevOps Copilot console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <div className="mx-auto max-w-7xl px-6 py-6">
          <header className="mb-8 flex items-center justify-between">
            <Link href="/" className="flex items-center gap-2">
              <span className="text-2xl font-semibold tracking-tight">Pilothouse</span>
              <span className="pill pill-info">console</span>
              <TenantBadge />
            </Link>
            <nav className="flex items-center gap-4 text-sm">
              <Link className="text-ink-200 hover:text-white" href="/dashboard">
                Dashboard
              </Link>
              <Link className="text-ink-200 hover:text-white" href="/agents">
                Agents
              </Link>
              <Link className="text-ink-200 hover:text-white" href="/runs">
                Runs
              </Link>
              <Link className="text-ink-200 hover:text-white" href="/approvals">
                Approvals
              </Link>
              <Link className="text-ink-200 hover:text-white" href="/schedule">
                Schedule
              </Link>
              <Link className="text-ink-200 hover:text-white" href="/plugins">
                Plugins
              </Link>
              <Link className="text-ink-200 hover:text-white" href="/system">
                System
              </Link>
            </nav>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
