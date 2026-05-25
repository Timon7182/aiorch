/**
 * Token usage types — mirrors `apps/backend/core/usage_event.py` and the
 * `/api/usage/*` response shape from `apps/web-server/server/routes/usage.py`.
 */

export interface UsageTotals {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cost_usd: number;
  calls: number;
}

export interface UsageEvent {
  phase: string | null;
  /** Set on session features (hermes, insights). Absent for agent runs. */
  feature?: string | null;
  model: string | null;
  agent?: string | null;
  subtask?: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cost_usd: number;
  timestamp: string;
}

export interface TaskUsage {
  taskId: string;
  projectId: string;
  specId: string;
  hasData: boolean;
  totals: UsageTotals;
  byPhase: Record<string, UsageTotals & { model?: string }>;
  byModel: Record<string, UsageTotals>;
  events: UsageEvent[];
  createdAt?: string;
  updatedAt?: string;
}

export interface ProjectTaskUsageSummary {
  taskId: string;
  specId: string;
  totals: UsageTotals;
  hasData: boolean;
  updatedAt?: string | null;
}

export interface ProjectUsage {
  projectId: string;
  totals: UsageTotals;
  byPhase: Record<string, UsageTotals>;
  byModel: Record<string, UsageTotals>;
  /**
   * Token usage grouped by feature: "agent" (per-spec task runs),
   * "hermes" (Hermes chat), "insights" (project Insights chat). Lets the
   * dashboard show where tokens are actually being spent.
   */
  byFeature: Record<string, UsageTotals>;
  tasks: ProjectTaskUsageSummary[];
  taskCount: number;
  tasksWithData: number;
}

export interface GlobalUsage {
  totals: UsageTotals;
  projects: Array<{
    projectId: string;
    name: string;
    totals: UsageTotals;
  }>;
}

export function emptyTotals(): UsageTotals {
  return {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    cost_usd: 0,
    calls: 0,
  };
}

/** Sum of every token kind — useful for compact displays. */
export function totalTokens(t: UsageTotals): number {
  return (
    t.input_tokens +
    t.output_tokens +
    t.cache_read_input_tokens +
    t.cache_creation_input_tokens
  );
}

/** Compact format: 1234 → "1.2K", 4_500_000 → "4.5M". */
export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(Math.round(n));
}

/** Format dollars: <$0.01 collapses to "<$0.01" so it's visible but tiny. */
export function formatCost(usd: number): string {
  if (!Number.isFinite(usd) || usd <= 0) return '$0.00';
  if (usd < 0.01) return '<$0.01';
  if (usd < 1) return `$${usd.toFixed(2)}`;
  if (usd < 100) return `$${usd.toFixed(2)}`;
  return `$${usd.toFixed(0)}`;
}
