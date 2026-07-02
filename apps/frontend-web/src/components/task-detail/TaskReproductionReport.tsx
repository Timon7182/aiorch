import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ScrollArea } from '../ui/scroll-area';
import type { Task, ReproductionReport } from '../../shared/types';

interface TaskReproductionReportProps {
  task: Task;
}

/**
 * Minimal viewer for a bug-reproduction report produced by QA.
 *
 * Fetches `<spec>/reproduction_report.md` (+ evidence list) via
 * `getReproductionReport` and renders the markdown. Evidence screenshots are
 * referenced by path in the report; their filenames are also listed below for
 * quick reference. Shown only for bug-report tasks.
 */
export function TaskReproductionReport({ task }: TaskReproductionReportProps) {
  const { t } = useTranslation(['tasks']);
  const [report, setReport] = useState<ReproductionReport | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      try {
        const result = await window.API.getReproductionReport(task.projectId, task.specId);
        if (!cancelled && result.success) {
          setReport(result.data ?? null);
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [task.projectId, task.specId]);

  const hasReport = report?.exists && report.content;

  return (
    <ScrollArea className="h-full">
      <div className="p-5 space-y-4">
        <h3 className="text-sm font-medium text-foreground">{t('tasks:reproduction.title')}</h3>

        {isLoading && !report && (
          <p className="text-xs text-muted-foreground">…</p>
        )}

        {!isLoading && !hasReport && (
          <p className="text-sm text-muted-foreground">{t('tasks:reproduction.empty')}</p>
        )}

        {hasReport && (
          <div className="prose prose-sm dark:prose-invert max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{report!.content!}</ReactMarkdown>
          </div>
        )}

        {hasReport && report!.evidence.length > 0 && (
          <div className="space-y-1.5">
            <h4 className="text-xs font-medium text-foreground">{t('tasks:reproduction.evidence')}</h4>
            <ul className="text-xs text-muted-foreground space-y-1">
              {report!.evidence.map((e) => (
                <li key={e.path} className="font-mono break-all">{e.name}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </ScrollArea>
  );
}
