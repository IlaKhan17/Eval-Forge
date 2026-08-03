import type { Config } from "tailwindcss"

export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Span types get stable colours so a waterfall is readable at a glance and
        // the same kind of operation always looks the same across traces.
        span: {
          agent: "#7c3aed",
          workflow: "#2563eb",
          llm: "#0891b2",
          tool: "#059669",
          retriever: "#ca8a04",
          embedding: "#c026d3",
          guardrail: "#dc2626",
          evaluator: "#4f46e5",
          custom: "#64748b",
        },
      },
    },
  },
} satisfies Config
