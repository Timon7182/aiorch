/**
 * Per-task token & cache usage tab.
 *
 * Shows the token economics of a single task's agent run: headline totals +
 * estimated cost, the input/output/cache-write/cache-read split (so you can
 * see how much was genuinely new vs. replayed from cache), and per-phase /
 * per-model breakdowns. Reads from the `/api/usage/tasks/{taskId}` endpoint
 * via the usage store, which also receives live `task:usage` WebSocket events.
 */

import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Coins, RefreshCw, Database, Sparkles } from 'lucide-react';

import { Button } from '../ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/card';
import { ScrollArea } from '../ui/scroll-area';
import {
  formatCost,
  formatTokens,
  totalTokens,
  type UsageTotals,
} from '../../shared/types/usage';
import { useUsageStore } from '../../stores/usage-store';
import type { Task } from '../../shared/types';

interface TaskUsageProps {
  task: Task;
}

const PHASE_LABEL_KEYS: Record<string, string> = {
  planning: 'tasks:usage.phases.planning',
  spec_creation: 'tasks:usage.phases.spec',
  coding: 'tasks:usage.phases.coding',
  qa_review: 'tasks:usage.phases.qa',
  validation: 'tasks:usage.phases.qa',
  qa_fixing: 'tasks:usage.phases.qaFix',
};

export function TaskUsage({ task }: TaskUsageProps) {
  const { t } = useTranslation(['tasks', 'common']);
  const usage = useUsageStore((s) => s.taskUsage[task.id]);
  const isLoading = useUsageStore((s) => s.loadingTask[task.id]);
  const fetchTaskUsage = useUsageStore((s) => s.fetchTaskUsage);

  useEffect(() => {
    if (task.id) fetchTaskUsage(task.id);
  }, [task.id, fetchTaskUsage]);

  const totals = usage?.totals;
  const total = totals ? totalTokens(totals) : 0;
  const hasAny = (totals?.calls ?? 0) > 0 && total > 0;

  // "New" = freshly processed (input + output + cache writes). The rest is
  // cache reads — the same context replayed across calls at a steep discount.
  const newTokens = totals
    ? totals.input_tokens +
      totals.output_tokens +
      totals.cache_creation_input_tokens
    : 0;
  const cachedTokens = totals?.cache_read_input_tokens ?? 0;
  const cachedPct = total > 0 ? (cachedTokens / total) * 100 : 0;

  return (
    <ScrollArea className="h-full">
      <div className="p-5 space-y-5">
        {/* Header */}
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 className="text-sm font-semibold flex items-center gap-2">
              <Coins className="h-4 w-4 text-amber-500" />
              {t('tasks:usage.taskTitle', 'Token & cache usage')}
            </h3>
            <p className="text-xs text-muted-foreground mt-1">
              {t(
                'tasks:usage.taskSubtitle',
                'Tokens consumed by the agents that built this task.',
              )}
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchTaskUsage(task.id)}
            disabled={isLoading}
          >
            <RefreshCw
              className={`h-3.5 w-3.5 mr-1.5 ${isLoading ? 'animate-spin' : ''}`}
            />
            {t('common:refresh', 'Refresh')}
          </Button>
        </div>

        {/* Empty state */}
        {!hasAny && (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">
              {isLoading
                ? t('common:loading', 'Loading…')
                : t(
                    'tasks:usage.taskEmpty',
                    'No token usage has been recorded for this task yet. It appears once the agents run.',
                  )}
            </CardContent>
          </Card>
        )}

        {hasAny && totals && (
          <>
            {/* Headline totals */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Stat
                label={t('tasks:usage.totalTokens', 'Total tokens')}
                value={formatTokens(total)}
              />
              <Stat
                label={t('tasks:usage.estCost', 'Est. cost')}
                value={formatCost(totals.cost_usd)}
                highlight
              />
              <Stat
                label={t('tasks:usage.calls', 'SDK calls')}
                value={String(totals.calls ?? 0)}
              />
              <Stat
                label={t('tasks:usage.cachedShare', 'Cached')}
                value={`${cachedPct.toFixed(0)}%`}
              />
            </div>

            {/* New vs. cached explainer */}
            <Card>
              <CardContent className="p-4 space-y-3">
                <div className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400">
                    <Sparkles className="h-3.5 w-3.5" />
                    {t('tasks:usage.newTokens', 'New')}:{' '}
                    <span className="font-mono font-semibold">
                      {formatTokens(newTokens)}
                    </span>
                  </span>
                  <span className="flex items-center gap-1.5 text-sky-600 dark:text-sky-400">
                    <Database className="h-3.5 w-3.5" />
                    {t('tasks:usage.cachedTokens', 'From cache')}:{' '}
                    <span className="font-mono font-semibold">
                      {formatTokens(cachedTokens)}
                    </span>
                  </span>
                </div>
                <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full bg-emerald-500"
                    style={{ width: `${100 - cachedPct}%` }}
                  />
                  <div
                    className="h-full bg-sky-500"
                    style={{ width: `${cachedPct}%` }}
                  />
                </div>
                <p className="text-[11px] text-muted-foreground leading-relaxed">
                  {t(
                    'tasks:usage.cacheNote',
                    'Cache reads are the same context replayed across calls, billed ~10× cheaper than fresh input — a high cached share means caching is saving you money, not wasting it.',
                  )}
                </p>
              </CardContent>
            </Card>

            {/* Token-type breakdown */}
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-semibold">
                  {t('tasks:usage.breakdown', 'Token breakdown')}
                </CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <div className="grid grid-cols-2 md:grid-cols-4 divide-y md:divide-y-0 md:divide-x">
                  <TokenCell
                    label={t('tasks:usage.input', 'Input')}
                    value={totals.input_tokens}
                  />
                  <TokenCell
                    label={t('tasks:usage.output', 'Output')}
                    value={totals.output_tokens}
                  />
                  <TokenCell
                    label={t('tasks:usage.cacheWrite', 'Cache write')}
                    value={totals.cache_creation_input_tokens}
                  />
                  <TokenCell
                    label={t('tasks:usage.cacheRead', 'Cache read')}
                    value={totals.cache_read_input_tokens}
                  />
                </div>
              </CardContent>
            </Card>

            {/* By phase */}
            {Object.keys(usage.byPhase ?? {}).length > 0 && (
              <Breakdown
                title={t('tasks:usage.byPhase', 'By phase')}
                rows={Object.entries(usage.byPhase).map(([k, v]) => ({
                  key: k,
                  label: t(PHASE_LABEL_KEYS[k] ?? '', k),
                  totals: v,
                }))}
                total={total}
              />
            )}

            {/* By model */}
            {Object.keys(usage.byModel ?? {}).length > 0 && (
              <Breakdown
                title={t('tasks:usage.byModel', 'By model')}
                rows={Object.entries(usage.byModel).map(([k, v]) => ({
                  key: k,
                  label: k,
                  totals: v,
                  mono: true,
                }))}
                total={total}
              />
            )}
          </>
        )}
      </div>
    </ScrollArea>
  );
}

function Stat({
  label,
  value,
  highlight,
}: {
  label: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <Card>
      <CardContent className="p-3">
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div
          className={[
            'mt-1 text-xl font-semibold font-mono tabular-nums',
            highlight ? 'text-amber-600 dark:text-amber-300' : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          {value}
        </div>
      </CardContent>
    </Card>
  );
}

function TokenCell({ label, value }: { label: string; value: number }) {
  return (
    <div className="p-4">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 font-mono font-semibold tabular-nums">
        {formatTokens(value)}
      </div>
    </div>
  );
}

function Breakdown({
  title,
  rows,
  total,
}: {
  title: string;
  rows: Array<{
    key: string;
    label: string;
    totals: UsageTotals;
    mono?: boolean;
  }>;
  total: number;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm font-semibold">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {rows.map((row) => {
          const rowTotal = totalTokens(row.totals);
          const pct = total > 0 ? (rowTotal / total) * 100 : 0;
          return (
            <div key={row.key} className="space-y-1">
              <div className="flex items-center justify-between gap-3 text-xs">
                <span
                  className={`truncate ${row.mono ? 'font-mono' : ''}`}
                  title={row.label}
                >
                  {row.label}
                </span>
                <div className="flex items-center gap-3 tabular-nums shrink-0">
                  <span className="font-mono">{formatTokens(rowTotal)}</span>
                  <span className="text-amber-600 dark:text-amber-300 font-semibold w-16 text-right">
                    {formatCost(row.totals.cost_usd)}
                  </span>
                </div>
              </div>
              <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full bg-amber-500"
                  style={{ width: `${Math.min(pct, 100)}%` }}
                />
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
