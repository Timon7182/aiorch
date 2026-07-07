import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { RefreshCw } from 'lucide-react';
import { ScrollArea } from '../ui/scroll-area';
import { Button } from '../ui/button';
import { cn } from '../../lib/utils';
import { getAuthToken } from '../../lib/auth';
import { useProjectStore } from '../../stores/project-store';
import type { Task, UiCheckReport, UiCheckVerdict } from '../../shared/types';

interface TaskUiCheckReportProps {
  task: Task;
}

/** Verdict → pill styling. Green = good outcome, red = problem found,
 *  amber = blocked/inconclusive. */
const VERDICT_STYLES: Record<UiCheckVerdict, string> = {
  PASS: 'bg-green-500/15 text-green-500',
  FIX_CONFIRMED: 'bg-green-500/15 text-green-500',
  BUG_NOT_REPRODUCED: 'bg-green-500/15 text-green-500',
  FAIL: 'bg-red-500/15 text-red-500',
  BUG_CONFIRMED: 'bg-red-500/15 text-red-500',
  FIX_FAILED: 'bg-red-500/15 text-red-500',
  BUG_INTERMITTENT: 'bg-amber-500/15 text-amber-500',
  BLOCKED: 'bg-amber-500/15 text-amber-500',
};

/**
 * Viewer for an on-demand UI-check report (taskType === 'ui_check').
 *
 * Fetches `<spec>/ui_check_report.md` + verdict + evidence list via
 * `getUiCheckReport`, renders the markdown with a verdict pill, and shows the
 * evidence screenshots inline (served through /api/files/serve).
 */
export function TaskUiCheckReport({ task }: TaskUiCheckReportProps) {
  const { t } = useTranslation(['tasks']);
  const [report, setReport] = useState<UiCheckReport | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const projects = useProjectStore(s => s.projects);
  const projectPath = projects.find(p => p.id === task.projectId)?.path;

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const result = await window.API.getUiCheckReport(task.projectId, task.specId);
      if (result.success) {
        setReport(result.data ?? null);
      }
    } finally {
      setIsLoading(false);
    }
  }, [task.projectId, task.specId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!cancelled) await load();
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  /** Absolute-path serve URL for an evidence screenshot. */
  const evidenceUrl = useCallback(
    (specRelativePath: string): string | null => {
      if (!projectPath || !task.specsPath) return null;
      const token = getAuthToken() || '';
      const params = new URLSearchParams({
        path: `${task.specsPath}/${specRelativePath}`,
        root: projectPath,
        token,
      });
      return `/api/files/serve?${params.toString()}`;
    },
    [projectPath, task.specsPath]
  );

  const hasReport = report?.exists && report.content;
  const verdict = report?.verdict ?? null;

  return (
    <ScrollArea className="h-full">
      <div className="p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h3 className="text-sm font-medium text-foreground">{t('tasks:uiCheck.title')}</h3>
            {verdict && (
              <span
                className={cn(
                  'text-xs font-semibold px-2 py-0.5 rounded-full',
                  VERDICT_STYLES[verdict] ?? 'bg-muted text-muted-foreground'
                )}
              >
                {verdict}
              </span>
            )}
          </div>
          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={load} disabled={isLoading}>
            <RefreshCw className={cn('h-3.5 w-3.5 mr-1', isLoading && 'animate-spin')} />
            {t('tasks:uiCheck.refresh')}
          </Button>
        </div>

        {isLoading && !report && (
          <p className="text-xs text-muted-foreground">{t('tasks:uiCheck.loading')}</p>
        )}

        {!isLoading && !hasReport && (
          <p className="text-sm text-muted-foreground">{t('tasks:uiCheck.empty')}</p>
        )}

        {hasReport && (
          <div className="prose prose-sm dark:prose-invert max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{report!.content!}</ReactMarkdown>
          </div>
        )}

        {hasReport && report!.evidence.length > 0 && (
          <div className="space-y-2">
            <h4 className="text-xs font-medium text-foreground">{t('tasks:uiCheck.evidence')}</h4>
            <div className="grid grid-cols-2 gap-3">
              {report!.evidence.map((e) => {
                const url = evidenceUrl(e.path);
                return (
                  <figure key={e.path} className="space-y-1">
                    {url ? (
                      <a href={url} target="_blank" rel="noopener noreferrer">
                        <img
                          src={url}
                          alt={e.name}
                          loading="lazy"
                          className="rounded-md border border-border max-w-full"
                        />
                      </a>
                    ) : null}
                    <figcaption className="text-xs text-muted-foreground font-mono break-all">
                      {e.name}
                    </figcaption>
                  </figure>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </ScrollArea>
  );
}
