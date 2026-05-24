/**
 * Compact dashboard card showing project-level token totals.
 * Sits at the top of KanbanBoard. Click to open the full UsageView.
 */

import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Coins, ArrowRight } from 'lucide-react';

import { Button } from './ui/button';
import { Card } from './ui/card';
import {
  formatCost,
  formatTokens,
  totalTokens,
} from '../shared/types/usage';
import { useUsageStore } from '../stores/usage-store';

interface ProjectUsageCardProps {
  projectId: string;
  onOpenDetails?: () => void;
}

export function ProjectUsageCard({
  projectId,
  onOpenDetails,
}: ProjectUsageCardProps) {
  const { t } = useTranslation(['tasks', 'common']);
  const usage = useUsageStore((s) => s.projectUsage[projectId]);
  const isLoading = useUsageStore((s) => s.loadingProject[projectId]);
  const fetchProjectUsage = useUsageStore((s) => s.fetchProjectUsage);

  useEffect(() => {
    if (projectId) {
      fetchProjectUsage(projectId);
    }
  }, [projectId, fetchProjectUsage]);

  if (!projectId) return null;

  const total = usage ? totalTokens(usage.totals) : 0;
  const hasAny = total > 0;

  return (
    <Card className="flex items-center gap-4 px-4 py-3 border-amber-500/20 bg-amber-500/5">
      <div className="flex h-9 w-9 items-center justify-center rounded-md bg-amber-500/10 text-amber-600 dark:text-amber-300">
        <Coins className="h-4 w-4" />
      </div>

      <div className="flex-1 min-w-0">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          {t('tasks:usage.projectTitle', 'Project token usage')}
        </div>
        {isLoading && !usage ? (
          <div className="mt-1 h-5 w-32 animate-pulse rounded bg-muted" />
        ) : hasAny ? (
          <div className="mt-0.5 flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <div className="flex items-baseline gap-1.5">
              <span className="font-mono font-semibold tabular-nums text-base">
                {formatTokens(total)}
              </span>
              <span className="text-[11px] text-muted-foreground">
                {t('tasks:usage.tokens', 'tokens')}
              </span>
            </div>
            <div className="flex items-baseline gap-1.5">
              <span className="font-mono font-semibold tabular-nums text-base text-amber-600 dark:text-amber-300">
                {formatCost(usage!.totals.cost_usd)}
              </span>
              <span className="text-[11px] text-muted-foreground">
                {t('tasks:usage.estCost', 'est. cost')}
              </span>
            </div>
            <div className="text-[11px] text-muted-foreground">
              {t('tasks:usage.acrossTasks', '{{count}} task(s) tracked', {
                count: usage!.tasksWithData,
              })}
            </div>
          </div>
        ) : (
          <div className="mt-0.5 text-xs text-muted-foreground">
            {t(
              'tasks:usage.noData',
              'No agent runs yet — token totals will appear here once a task runs.',
            )}
          </div>
        )}
      </div>

      {onOpenDetails && hasAny && (
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1.5"
          onClick={onOpenDetails}
        >
          {t('tasks:usage.viewDetails', 'Details')}
          <ArrowRight className="h-3.5 w-3.5" />
        </Button>
      )}
    </Card>
  );
}
