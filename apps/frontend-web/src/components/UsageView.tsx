/**
 * Full-page token-usage analytics for the selected project.
 *
 * Renders three sections:
 *   - Headline totals (tokens + estimated cost + tracked task count)
 *   - Per-phase breakdown (planner / coder / qa_review / qa_fixing)
 *   - Per-task sortable table with input/output/cache columns and live event chart
 */

import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Coins,
  RefreshCw,
  ArrowUpDown,
  ArrowDown,
  ArrowUp,
} from 'lucide-react';

import { Button } from './ui/button';
import { Card, CardContent, CardHeader, CardTitle } from './ui/card';
import { ScrollArea } from './ui/scroll-area';
import { ClaudeCliUsageCard } from './ClaudeCliUsageCard';
import {
  formatCost,
  formatTokens,
  totalTokens,
  type UsageTotals,
} from '../shared/types/usage';
import { useUsageStore } from '../stores/usage-store';

interface UsageViewProps {
  projectId: string;
}

type SortKey =
  | 'spec'
  | 'total'
  | 'input'
  | 'output'
  | 'cache_read'
  | 'cache_write'
  | 'cost'
  | 'updated';

interface SortState {
  key: SortKey;
  dir: 'asc' | 'desc';
}

const PHASE_LABEL_KEYS: Record<string, string> = {
  planning: 'tasks:usage.phases.planning',
  spec_creation: 'tasks:usage.phases.spec',
  coding: 'tasks:usage.phases.coding',
  qa_review: 'tasks:usage.phases.qa',
  validation: 'tasks:usage.phases.qa',
  qa_fixing: 'tasks:usage.phases.qaFix',
};

const FEATURE_LABEL_KEYS: Record<string, string> = {
  agent: 'tasks:usage.features.agent',
  hermes: 'tasks:usage.features.hermes',
  insights: 'tasks:usage.features.insights',
  session: 'tasks:usage.features.session',
};

export function UsageView({ projectId }: UsageViewProps) {
  const { t } = useTranslation(['tasks', 'common']);
  const usage = useUsageStore((s) => s.projectUsage[projectId]);
  const isLoading = useUsageStore((s) => s.loadingProject[projectId]);
  const fetchProjectUsage = useUsageStore((s) => s.fetchProjectUsage);
  const [sort, setSort] = useState<SortState>({ key: 'total', dir: 'desc' });

  useEffect(() => {
    if (projectId) fetchProjectUsage(projectId);
  }, [projectId, fetchProjectUsage]);

  const sortedTasks = useMemo(() => {
    if (!usage?.tasks) return [];
    const tasks = [...usage.tasks];
    const dir = sort.dir === 'asc' ? 1 : -1;
    tasks.sort((a, b) => {
      const av = sortValue(a.totals, a.specId, a.updatedAt, sort.key);
      const bv = sortValue(b.totals, b.specId, b.updatedAt, sort.key);
      if (av < bv) return -1 * dir;
      if (av > bv) return 1 * dir;
      return 0;
    });
    return tasks;
  }, [usage, sort]);

  const toggleSort = (key: SortKey) => {
    setSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : { key, dir: key === 'spec' ? 'asc' : 'desc' },
    );
  };

  if (!projectId) return null;

  const totals = usage?.totals;
  const total = totals ? totalTokens(totals) : 0;
  const hasAny = total > 0;

  return (
    <ScrollArea className="h-full">
      <div className="mx-auto max-w-6xl p-4 md:p-6 space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
              <Coins className="h-5 w-5 text-amber-500" />
              {t('tasks:usage.pageTitle', 'Token Usage')}
            </h1>
            <p className="text-sm text-muted-foreground mt-1">
              {t(
                'tasks:usage.pageSubtitle',
                'Per-task and per-project token consumption across all agent runs in this project.',
              )}
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchProjectUsage(projectId)}
            disabled={isLoading}
          >
            <RefreshCw
              className={`h-3.5 w-3.5 mr-1.5 ${isLoading ? 'animate-spin' : ''}`}
            />
            {t('common:refresh', 'Refresh')}
          </Button>
        </div>

        {/* Claude CLI session/weekly limits (parity with `claude /usage`) */}
        <ClaudeCliUsageCard />

        {/* Headline totals */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <HeadlineStat
            label={t('tasks:usage.totalTokens', 'Total tokens')}
            value={formatTokens(total)}
            mono
          />
          <HeadlineStat
            label={t('tasks:usage.estCost', 'Est. cost')}
            value={formatCost(totals?.cost_usd ?? 0)}
            mono
            highlight
          />
          <HeadlineStat
            label={t('tasks:usage.calls', 'SDK calls')}
            value={String(totals?.calls ?? 0)}
            mono
          />
          <HeadlineStat
            label={t('tasks:usage.tracked', 'Tasks tracked')}
            value={`${usage?.tasksWithData ?? 0} / ${usage?.taskCount ?? 0}`}
          />
        </div>

        {/* Empty state */}
        {!hasAny && (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">
              {isLoading
                ? t('common:loading', 'Loading…')
                : t(
                    'tasks:usage.empty',
                    'No agent runs have been recorded for this project yet. Start a task to populate usage data.',
                  )}
            </CardContent>
          </Card>
        )}

        {hasAny && (
          <>
            {/* Per-feature breakdown — shows where tokens are spent
                across agent runs vs Hermes vs Insights chat. */}
            {usage && Object.keys(usage.byFeature ?? {}).length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-sm font-semibold">
                    {t('tasks:usage.byFeature', 'By feature')}
                  </CardTitle>
                </CardHeader>
                <CardContent className="p-0">
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 divide-y sm:divide-y-0 sm:divide-x">
                    {Object.entries(usage.byFeature).map(
                      ([feature, featureTotals]) => {
                        const featureTotal = totalTokens(featureTotals);
                        const pct = total > 0 ? (featureTotal / total) * 100 : 0;
                        return (
                          <div key={feature} className="p-4 space-y-2">
                            <div className="text-xs uppercase tracking-wide text-muted-foreground">
                              {t(FEATURE_LABEL_KEYS[feature] ?? '', feature)}
                            </div>
                            <div className="font-mono font-semibold tabular-nums">
                              {formatTokens(featureTotal)}
                            </div>
                            <div className="text-xs text-muted-foreground">
                              {formatCost(featureTotals.cost_usd)} ·{' '}
                              {pct.toFixed(0)}%
                            </div>
                            <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                              <div
                                className="h-full bg-amber-500"
                                style={{ width: `${Math.min(pct, 100)}%` }}
                              />
                            </div>
                          </div>
                        );
                      },
                    )}
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Per-phase breakdown */}
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-semibold">
                  {t('tasks:usage.byPhase', 'By phase')}
                </CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 divide-y sm:divide-y-0 sm:divide-x">
                  {Object.entries(usage!.byPhase).map(([phase, phaseTotals]) => {
                    const phaseTotal = totalTokens(phaseTotals);
                    const pct = total > 0 ? (phaseTotal / total) * 100 : 0;
                    return (
                      <div key={phase} className="p-4 space-y-2">
                        <div className="text-xs uppercase tracking-wide text-muted-foreground">
                          {t(PHASE_LABEL_KEYS[phase] ?? '', phase)}
                        </div>
                        <div className="font-mono font-semibold tabular-nums">
                          {formatTokens(phaseTotal)}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {formatCost(phaseTotals.cost_usd)} ·{' '}
                          {pct.toFixed(0)}%
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
                </div>
              </CardContent>
            </Card>

            {/* Per-model breakdown (compact) */}
            {Object.keys(usage!.byModel).length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-sm font-semibold">
                    {t('tasks:usage.byModel', 'By model')}
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm">
                  {Object.entries(usage!.byModel).map(([model, mTotals]) => (
                    <div
                      key={model}
                      className="flex items-center justify-between gap-4 py-1"
                    >
                      <span className="font-mono text-xs truncate" title={model}>
                        {model}
                      </span>
                      <div className="flex items-center gap-4 text-xs tabular-nums">
                        <span className="text-muted-foreground">
                          {mTotals.calls} {t('tasks:usage.calls', 'calls')}
                        </span>
                        <span>{formatTokens(totalTokens(mTotals))}</span>
                        <span className="text-amber-600 dark:text-amber-300 font-semibold w-20 text-right">
                          {formatCost(mTotals.cost_usd)}
                        </span>
                      </div>
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            {/* Per-task table */}
            <Card>
              <CardHeader>
                <CardTitle className="text-sm font-semibold">
                  {t('tasks:usage.byTask', 'By task')}
                </CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="border-b bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
                      <tr>
                        <Th sort={sort} k="spec" onClick={toggleSort}>
                          {t('tasks:usage.cols.spec', 'Spec')}
                        </Th>
                        <Th sort={sort} k="total" onClick={toggleSort} right>
                          {t('tasks:usage.cols.total', 'Total')}
                        </Th>
                        <Th sort={sort} k="input" onClick={toggleSort} right>
                          {t('tasks:usage.cols.input', 'Input')}
                        </Th>
                        <Th sort={sort} k="output" onClick={toggleSort} right>
                          {t('tasks:usage.cols.output', 'Output')}
                        </Th>
                        <Th sort={sort} k="cache_read" onClick={toggleSort} right>
                          {t('tasks:usage.cols.cacheRead', 'Cache R')}
                        </Th>
                        <Th sort={sort} k="cache_write" onClick={toggleSort} right>
                          {t('tasks:usage.cols.cacheWrite', 'Cache W')}
                        </Th>
                        <Th sort={sort} k="cost" onClick={toggleSort} right>
                          {t('tasks:usage.cols.cost', 'Cost')}
                        </Th>
                        <Th sort={sort} k="updated" onClick={toggleSort} right>
                          {t('tasks:usage.cols.updated', 'Updated')}
                        </Th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortedTasks.map((task) => (
                        <tr
                          key={task.taskId}
                          className="border-b last:border-0 hover:bg-muted/30 transition-colors"
                        >
                          <td className="px-3 py-2 font-mono text-xs">
                            {task.specId}
                          </td>
                          <Td value={formatTokens(totalTokens(task.totals))} mono />
                          <Td value={formatTokens(task.totals.input_tokens)} mono />
                          <Td value={formatTokens(task.totals.output_tokens)} mono />
                          <Td
                            value={formatTokens(
                              task.totals.cache_read_input_tokens,
                            )}
                            mono
                          />
                          <Td
                            value={formatTokens(
                              task.totals.cache_creation_input_tokens,
                            )}
                            mono
                          />
                          <td className="px-3 py-2 text-right font-mono tabular-nums font-semibold text-amber-600 dark:text-amber-300">
                            {formatCost(task.totals.cost_usd)}
                          </td>
                          <td className="px-3 py-2 text-right text-xs text-muted-foreground">
                            {formatTimestamp(task.updatedAt)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </CardContent>
            </Card>
          </>
        )}
      </div>
    </ScrollArea>
  );
}

function HeadlineStat({
  label,
  value,
  mono,
  highlight,
}: {
  label: string;
  value: string;
  mono?: boolean;
  highlight?: boolean;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
          {label}
        </div>
        <div
          className={[
            'mt-1 text-2xl font-semibold',
            mono ? 'font-mono tabular-nums' : '',
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

function Th({
  children,
  k,
  sort,
  onClick,
  right,
}: {
  children: React.ReactNode;
  k: SortKey;
  sort: SortState;
  onClick: (k: SortKey) => void;
  right?: boolean;
}) {
  const active = sort.key === k;
  const Icon = !active ? ArrowUpDown : sort.dir === 'asc' ? ArrowUp : ArrowDown;
  return (
    <th
      className={`px-3 py-2 ${right ? 'text-right' : 'text-left'} cursor-pointer select-none`}
      onClick={() => onClick(k)}
    >
      <span
        className={`inline-flex items-center gap-1 ${
          right ? 'flex-row-reverse' : ''
        } ${active ? 'text-foreground' : ''}`}
      >
        <Icon className="h-3 w-3 opacity-60" />
        {children}
      </span>
    </th>
  );
}

function Td({ value, mono }: { value: string; mono?: boolean }) {
  return (
    <td
      className={`px-3 py-2 text-right ${mono ? 'font-mono tabular-nums' : ''}`}
    >
      {value}
    </td>
  );
}

function sortValue(
  totals: UsageTotals,
  spec: string,
  updatedAt: string | null | undefined,
  key: SortKey,
): number | string {
  switch (key) {
    case 'spec':
      return spec;
    case 'total':
      return totalTokens(totals);
    case 'input':
      return totals.input_tokens;
    case 'output':
      return totals.output_tokens;
    case 'cache_read':
      return totals.cache_read_input_tokens;
    case 'cache_write':
      return totals.cache_creation_input_tokens;
    case 'cost':
      return totals.cost_usd;
    case 'updated':
      return updatedAt ? Date.parse(updatedAt) || 0 : 0;
  }
}

function formatTimestamp(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString();
}
