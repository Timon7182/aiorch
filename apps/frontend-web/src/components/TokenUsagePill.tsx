/**
 * Compact token-usage chip rendered on TaskCard.
 *
 * Lazy-loads usage from `/api/usage/tasks/{taskId}` on first hover so the
 * Kanban board does not fire one HTTP request per card on mount. Live
 * `task:usage` WebSocket events keep the chip current after the first fetch.
 */

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Coins } from 'lucide-react';

import { Badge } from './ui/badge';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from './ui/tooltip';
import { formatCost, formatTokens, totalTokens } from '../shared/types/usage';
import { useUsageStore } from '../stores/usage-store';

interface TokenUsagePillProps {
  taskId: string;
  /** When true, fetches eagerly on mount (used in task detail view). */
  eager?: boolean;
}

export function TokenUsagePill({ taskId, eager = false }: TokenUsagePillProps) {
  const { t } = useTranslation(['tasks', 'common']);
  const usage = useUsageStore((s) => s.taskUsage[taskId]);
  const isLoading = useUsageStore((s) => s.loadingTask[taskId]);
  const fetchTaskUsage = useUsageStore((s) => s.fetchTaskUsage);
  const [hasFetched, setHasFetched] = useState(false);
  // Avoid re-fetching every render — once we've kicked off a fetch, the store
  // owns the lifecycle.
  const fetchedRef = useRef(false);

  useEffect(() => {
    if (eager && !fetchedRef.current) {
      fetchedRef.current = true;
      setHasFetched(true);
      fetchTaskUsage(taskId);
    }
  }, [eager, taskId, fetchTaskUsage]);

  const triggerFetch = () => {
    if (!fetchedRef.current) {
      fetchedRef.current = true;
      setHasFetched(true);
      fetchTaskUsage(taskId);
    }
  };

  // Hide entirely until we know there's data. Empty pills clutter cards.
  if (!usage?.hasData) {
    // First-time hover triggers the fetch; once we know there's no data we
    // stay hidden until the next live event arrives.
    if (!hasFetched && !isLoading) {
      return (
        <span
          onMouseEnter={triggerFetch}
          onFocus={triggerFetch}
          className="sr-only"
          aria-hidden
        />
      );
    }
    return null;
  }

  const total = totalTokens(usage.totals);
  const cost = formatCost(usage.totals.cost_usd);

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge
            variant="outline"
            className="text-[10px] px-1.5 py-0 gap-1 font-mono tabular-nums border-amber-500/30 text-amber-700 dark:text-amber-300 bg-amber-500/5"
          >
            <Coins className="h-2.5 w-2.5" />
            {formatTokens(total)}
            <span className="opacity-60">·</span>
            {cost}
          </Badge>
        </TooltipTrigger>
        <TooltipContent side="top" className="text-xs w-64">
          <div className="space-y-2">
            <div className="font-medium">{t('tasks:usage.title', 'Token usage')}</div>
            <div className="space-y-1">
              <Row label={t('tasks:usage.input', 'Input')} value={formatTokens(usage.totals.input_tokens)} />
              <Row label={t('tasks:usage.output', 'Output')} value={formatTokens(usage.totals.output_tokens)} />
              <Row
                label={t('tasks:usage.cacheRead', 'Cache read')}
                value={formatTokens(usage.totals.cache_read_input_tokens)}
              />
              <Row
                label={t('tasks:usage.cacheWrite', 'Cache write')}
                value={formatTokens(usage.totals.cache_creation_input_tokens)}
              />
            </div>
            <div className="h-px bg-border" />
            <Row
              label={t('tasks:usage.calls', 'SDK calls')}
              value={String(usage.totals.calls)}
            />
            <Row
              label={t('tasks:usage.cost', 'Est. cost')}
              value={cost}
              accent
            />
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function Row({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-muted-foreground">{label}</span>
      <span
        className={
          accent
            ? 'font-semibold text-primary tabular-nums'
            : 'font-medium tabular-nums'
        }
      >
        {value}
      </span>
    </div>
  );
}
