import { QueryProvider } from "@/components/QueryProvider"
import type { Metadata } from "next"
import Link from "next/link"
import type { ReactNode } from "react"
import "./globals.css"

export const metadata: Metadata = {
  title: "Proofstep",
  description: "Evaluation CI and trajectory testing for production AI agents",
  // No indexing: a dashboard reachable from the internet should not end up in a search
  // index, and trace names alone can disclose more than anyone intended.
  robots: { index: false, follow: false },
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <QueryProvider>
          <div className="flex min-h-screen flex-col">
            <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur">
              <div className="mx-auto flex max-w-[1600px] items-center gap-6 px-6 py-3">
                <Link href="/traces" className="font-semibold tracking-tight text-slate-100">
                  Proofstep
                </Link>
                <nav className="flex gap-4 text-sm text-slate-400">
                  <Link href="/traces" className="hover:text-slate-100">
                    Traces
                  </Link>
                  <Link href="/experiments" className="hover:text-slate-100">
                    Experiments
                  </Link>
                  <Link href="/settings/keys" className="hover:text-slate-100">
                    Settings
                  </Link>
                </nav>
              </div>
            </header>
            <main className="mx-auto w-full max-w-[1600px] flex-1 px-6 py-6">{children}</main>
          </div>
        </QueryProvider>
      </body>
    </html>
  )
}
