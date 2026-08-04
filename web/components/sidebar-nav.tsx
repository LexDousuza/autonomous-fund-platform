"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/portfolio", label: "Portfolio" },
  { href: "/strategies", label: "Strategies" },
  { href: "/orders", label: "Orders" },
  { href: "/trades", label: "Trade History" },
  { href: "/risk", label: "Risk" },
  { href: "/analytics", label: "Analytics" },
  { href: "/market", label: "Market" },
  { href: "/watchlist", label: "Watchlist" },
];

export function SidebarNav() {
  const pathname = usePathname();

  return (
    <nav className="w-48 shrink-0 border-r border-neutral-200 dark:border-neutral-800 p-4 space-y-1">
      {LINKS.map((link) => {
        const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
        return (
          <Link
            key={link.href}
            href={link.href}
            className={`block rounded-lg px-3 py-2 text-sm ${
              active
                ? "bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-900"
                : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-900"
            }`}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
