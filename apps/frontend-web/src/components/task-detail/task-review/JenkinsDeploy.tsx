import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, ExternalLink, AlertTriangle, Terminal, ChevronDown, ChevronRight, Factory, CheckCircle2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../ui/button';
import { cn } from '../../../lib/utils';
import type { JenkinsState, JenkinsStatus } from '../../../shared/types';

interface JenkinsDeployProps {
  taskId: string;
}

const MAX_LOG_LINES = 500;
const TRANSIENT: JenkinsStatus[] = ['bumping', 'publishing', 'pushing', 'triggering', 'queued', 'building'];

/**
 * Self-contained "Deploy on Jenkins" panel: publishes the task's library changes
 * (version bump + gradle uploadArchives), pushes the task branches to the git
 * remote, triggers the project's parameterized Jenkins job and follows the build
 * to its result. Hidden entirely for projects without a jenkins section in
 * deploy.config.json. Modeled on PreviewDeploy (window.API directly + polling +
 * WebSocket log/status streaming).
 */
export function JenkinsDeploy({ taskId }: JenkinsDeployProps) {
  const { t } = useTranslation(['tasks', 'common']);
  const [state, setState] = useState<JenkinsState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [showLogs, setShowLogs] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const res = await window.API.getJenkins(taskId);
      if (res.success && res.data) {
        setState(res.data);
        if (!TRANSIENT.includes(res.data.status)) stopPolling();
      }
    } catch {
      // transient network errors during polling are non-fatal
    }
  }, [taskId, stopPolling]);

  const startPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(refresh, 3000);
  }, [refresh, stopPolling]);

  // initial load + cleanup
  useEffect(() => {
    refresh();
    return stopPolling;
  }, [refresh, stopPolling]);

  // keep polling whenever the status is transient
  useEffect(() => {
    if (state && TRANSIENT.includes(state.status)) startPolling();
    else stopPolling();
  }, [state, startPolling, stopPolling]);

  // Live status + log streaming over the global events WebSocket. Complements
  // (does not replace) the polling above — either one keeps the UI current.
  useEffect(() => {
    const offStatus = window.API.onJenkinsStatus?.((data) => {
      if (data.taskId !== taskId) return;
      setState((prev) => ({
        ...(prev ?? { status: 'none' }),
        status: data.status,
        buildUrl: data.buildUrl ?? (prev?.buildUrl ?? null),
        error: data.error ?? null,
        enabled: true,
      }));
    });
    const offLog = window.API.onJenkinsLog?.((data) => {
      if (data.taskId !== taskId) return;
      setLogs((prev) => {
        const next = [...prev, data.line];
        return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next;
      });
    });
    return () => {
      offStatus?.();
      offLog?.();
    };
  }, [taskId]);

  // Auto-scroll the log panel to the newest line while it's open.
  useEffect(() => {
    if (showLogs) logEndRef.current?.scrollIntoView({ block: 'end' });
  }, [logs, showLogs]);

  const handleDeploy = async () => {
    setBusy(true);
    setError(null);
    setLogs([]);
    try {
      const res = await window.API.deployJenkins(taskId);
      if (res.success && res.data) {
        setState(res.data);
        setShowLogs(true);
        startPolling();
      } else {
        setError(res.error || t('tasks:jenkins.deployFailed'));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Projects without a jenkins config never show the panel.
  if (!state || state.enabled === false) return null;

  const status = state.status ?? 'none';
  const isTransient = TRANSIENT.includes(status);
  const showDeployButton = !isTransient;

  const statusLabel: Record<string, string> = {
    bumping: t('tasks:jenkins.bumping'),
    publishing: t('tasks:jenkins.publishing'),
    pushing: t('tasks:jenkins.pushing'),
    triggering: t('tasks:jenkins.triggering'),
    queued: t('tasks:jenkins.queued'),
    building: t('tasks:jenkins.building'),
    success: t('tasks:jenkins.success'),
    failed: t('tasks:jenkins.failed'),
  };

  return (
    <div className="rounded-md border border-border/60 bg-muted/30 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Factory className="h-4 w-4 text-primary" />
          {t('tasks:jenkins.title')}
          {state.branch && (
            <span className="text-xs font-normal text-muted-foreground">({state.branch})</span>
          )}
        </div>
        {status !== 'none' && (
          <span
            className={cn(
              'text-xs px-2 py-0.5 rounded-full',
              status === 'success' && 'bg-success/15 text-success',
              status === 'failed' && 'bg-destructive/15 text-destructive',
              isTransient && 'bg-primary/15 text-primary',
            )}
          >
            {statusLabel[status] ?? status}
          </span>
        )}
      </div>

      {/* Published library version (when the library step ran) */}
      {state.libVersion != null && (
        <div className="text-xs text-muted-foreground">
          {t('tasks:jenkins.libVersion', { version: state.libVersion })}
        </div>
      )}

      {/* Link to the Jenkins build once one exists */}
      {state.buildUrl && (
        <a
          href={state.buildUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 text-sm text-primary hover:underline break-all"
        >
          <ExternalLink className="h-3.5 w-3.5 shrink-0" />
          {t('tasks:jenkins.build', { number: state.buildNumber ?? '' })} {state.buildUrl}
        </a>
      )}

      {status === 'success' && (
        <div className="flex items-center gap-1.5 text-xs text-success">
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
          {t('tasks:jenkins.deployed')}
        </div>
      )}

      {(error || (status === 'failed' && state.error)) && (
        <div className="flex items-start gap-1.5 text-xs text-destructive">
          <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span className="break-words">{error || state.error}</span>
        </div>
      )}

      <div className="flex gap-2">
        {showDeployButton && (
          <Button type="button" size="sm" onClick={handleDeploy} disabled={busy} className="flex-1">
            {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Factory className="mr-2 h-4 w-4" />}
            {status === 'failed' || status === 'success' ? t('tasks:jenkins.redeploy') : t('tasks:jenkins.deploy')}
          </Button>
        )}
        {isTransient && (
          <Button type="button" size="sm" variant="outline" disabled className="flex-1">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            {statusLabel[status]}
          </Button>
        )}
      </div>

      {/* Streamed publish/push/build progress */}
      {logs.length > 0 && (
        <div className="rounded-md border border-border/60 overflow-hidden">
          <button
            type="button"
            onClick={() => setShowLogs((v) => !v)}
            className="flex w-full items-center gap-1.5 px-2 py-1 text-xs font-medium hover:bg-muted/50"
          >
            {showLogs ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            <Terminal className="h-3.5 w-3.5" />
            {t('tasks:jenkins.logs')} ({logs.length})
          </button>
          {showLogs && (
            <div className="max-h-48 overflow-auto bg-black/90 px-2 py-1 font-mono text-[11px] leading-tight text-green-200">
              {logs.map((line, i) => (
                <div key={i} className="whitespace-pre-wrap break-all">{line}</div>
              ))}
              <div ref={logEndRef} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default JenkinsDeploy;
