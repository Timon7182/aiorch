import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, Rocket, ExternalLink, Square, ArrowUpCircle, AlertTriangle, Clock, TimerReset, Terminal, ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../ui/button';
import { cn } from '../../../lib/utils';
import type { PreviewState, PreviewStrategy } from '../../../shared/types';

interface PreviewDeployProps {
  taskId: string;
}

type StrategyChoice = 'auto' | PreviewStrategy;
const LOCAL_STRATEGIES: PreviewStrategy[] = ['dev-server', 'compose-local'];
const MAX_LOG_LINES = 500;

const TRANSIENT: PreviewState['status'][] = ['building', 'deploying', 'promoting'];
// Show the "need more time?" banner once the preview has this many seconds or fewer left.
const EXPIRY_WARN_SECONDS = 15 * 60;

/** Format a remaining-seconds count as "1h 12m" / "47m" / "<1m". */
function formatRemaining(seconds: number): string {
  if (seconds <= 0) return '0m';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return '<1m';
}

/**
 * Self-contained "Run on server" panel: deploy the finished task's worktree to an
 * isolated preview on the deploy host, show its live URL/IP, and Stop / Promote it.
 * Polls while a deploy/teardown is in flight. Uses window.API directly (like
 * CreatePRDialog) to avoid threading props through the whole task-detail tree.
 */
export function PreviewDeploy({ taskId }: PreviewDeployProps) {
  const { t } = useTranslation(['tasks', 'common']);
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Which golden DB the preview clones from: 'auto' = derive from branch,
  // 'A' = main/pre-prod data, 'B' = test data.
  const [lane, setLane] = useState<'auto' | 'A' | 'B'>('auto');
  // Which preview strategy to use. 'auto' defers to the project's deploy.config.json.
  const [strategy, setStrategy] = useState<StrategyChoice>('auto');
  // Streamed preview:log lines (dev-server / compose build output), capped.
  const [logs, setLogs] = useState<string[]>([]);
  const [showLogs, setShowLogs] = useState(false);
  // Ticks every second to drive the live "expires in …" countdown.
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  // The effective strategy of the current/pending preview: prefer what the
  // backend reported, else the explicit selection, else remote.
  const effectiveStrategy: PreviewStrategy =
    (preview?.strategy as PreviewStrategy | undefined) ??
    (strategy !== 'auto' ? strategy : 'docker-remote');
  const isLocal = LOCAL_STRATEGIES.includes(effectiveStrategy);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const res = await window.API.getPreview(taskId);
      if (res.success && res.data) {
        setPreview(res.data);
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
    if (preview && TRANSIENT.includes(preview.status)) startPolling();
    else stopPolling();
  }, [preview, startPolling, stopPolling]);

  // While running, tick the countdown every second and re-fetch occasionally so we
  // notice when the reaper tears the preview down (status -> stopped).
  useEffect(() => {
    if (preview?.status !== 'running') return;
    const tick = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1000);
    const poll = setInterval(refresh, 30000);
    return () => {
      clearInterval(tick);
      clearInterval(poll);
    };
  }, [preview?.status, refresh]);

  // Live status + log streaming over the global events WebSocket. Complements
  // (does not replace) the polling above — either one keeps the UI current.
  useEffect(() => {
    const offStatus = window.API.onPreviewStatus?.((data) => {
      if (data.taskId !== taskId) return;
      setPreview((prev) => ({
        ...(prev ?? { status: 'none' }),
        status: data.status,
        strategy: data.strategy,
        url: data.url ?? (prev?.url ?? null),
        error: data.error ?? null,
      }));
    });
    const offLog = window.API.onPreviewLog?.((data) => {
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
      const opts: { lane?: string; strategy?: PreviewStrategy } = {};
      if (strategy !== 'auto') opts.strategy = strategy;
      // Lane (golden DB) only applies to the remote docker strategy.
      if (!LOCAL_STRATEGIES.includes(strategy as PreviewStrategy) && lane !== 'auto') opts.lane = lane;
      const res = await window.API.deployPreview(
        taskId,
        Object.keys(opts).length ? opts : undefined,
      );
      if (res.success && res.data) {
        setPreview(res.data);
        setShowLogs(true);
        startPolling();
      } else {
        setError(res.error || t('tasks:preview.deployFailed'));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleStop = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await window.API.stopPreview(taskId);
      if (res.success && res.data) setPreview(res.data);
      else setError(res.error || t('tasks:preview.stopFailed'));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleExtend = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await window.API.extendPreview(taskId, { hours: 1 });
      if (res.success && res.data) setPreview(res.data);
      else setError(res.error || t('tasks:preview.extendFailed'));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handlePromote = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await window.API.promotePreview(taskId);
      if (res.success && res.data) setPreview(res.data);
      else setError(res.error || t('tasks:preview.promoteFailed'));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const status = preview?.status ?? 'none';
  const isTransient = TRANSIENT.includes(status as PreviewState['status']);
  const showDeployButton = status === 'none' || status === 'stopped' || status === 'failed' || status === 'promoted';

  // Countdown / expiry (only meaningful while running with a known expiry).
  const expiresAt = status === 'running' ? preview?.expiresAt ?? null : null;
  const remaining = expiresAt != null ? expiresAt - nowSec : null;
  const showExpiryWarning = remaining != null && remaining <= EXPIRY_WARN_SECONDS;

  const statusLabel: Record<string, string> = {
    building: t('tasks:preview.building'),
    deploying: t('tasks:preview.deploying'),
    promoting: t('tasks:preview.promoting'),
    running: t('tasks:preview.running'),
    failed: t('tasks:preview.failed'),
    stopped: t('tasks:preview.stopped'),
    promoted: t('tasks:preview.promoted'),
  };

  return (
    <div className="rounded-md border border-border/60 bg-muted/30 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Rocket className="h-4 w-4 text-primary" />
          {t('tasks:preview.title')}
        </div>
        {status !== 'none' && (
          <span
            className={cn(
              'text-xs px-2 py-0.5 rounded-full',
              status === 'running' && 'bg-success/15 text-success',
              status === 'failed' && 'bg-destructive/15 text-destructive',
              isTransient && 'bg-primary/15 text-primary',
              (status === 'stopped' || status === 'promoted') && 'bg-muted text-muted-foreground',
            )}
          >
            {statusLabel[status] ?? status}
            {preview?.lane ? ` · ${t('tasks:preview.lane')} ${preview.lane}` : ''}
          </span>
        )}
      </div>

      {/* Running: show the live URL + actions */}
      {status === 'running' && preview?.url && (
        <a
          href={preview.url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 text-sm text-primary hover:underline break-all"
        >
          <ExternalLink className="h-3.5 w-3.5 shrink-0" />
          {preview.url}
        </a>
      )}

      {/* Countdown while running (and a known expiry) */}
      {remaining != null && !showExpiryWarning && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Clock className="h-3.5 w-3.5 shrink-0" />
          {t('tasks:preview.expiresIn', { time: formatRemaining(remaining) })}
        </div>
      )}

      {/* Expiry warning: "need more time?" with a one-click +1h extend */}
      {showExpiryWarning && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5">
          <div className="flex items-center gap-1.5 text-xs text-warning-foreground">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>
              {remaining != null && remaining > 0
                ? `${t('tasks:preview.expiresSoon')} (${formatRemaining(remaining)})`
                : t('tasks:preview.expired')}
            </span>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={handleExtend} disabled={busy} className="h-7 shrink-0">
            {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <TimerReset className="mr-1.5 h-3.5 w-3.5" />}
            {t('tasks:preview.extend')}
          </Button>
        </div>
      )}

      {/* Promoted: show the static URL */}
      {status === 'promoted' && preview?.staticUrl && (
        <div className="text-sm text-muted-foreground break-all">
          {t('tasks:preview.promotedTo')}{' '}
          <a href={preview.staticUrl} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
            {preview.staticUrl}
          </a>
        </div>
      )}

      {(error || (status === 'failed' && preview?.error)) && (
        <div className="flex items-start gap-1.5 text-xs text-destructive">
          <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span className="break-words">{error || preview?.error}</span>
        </div>
      )}

      {/* Strategy chooser — how the preview runs (local dev-server / local compose / remote docker) */}
      {showDeployButton && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">{t('tasks:preview.strategy')}</span>
          <div className="inline-flex flex-wrap rounded-md border border-border/60 overflow-hidden">
            {([
              ['auto', t('tasks:preview.strategyAuto')],
              ['dev-server', t('tasks:preview.strategyDevServer')],
              ['compose-local', t('tasks:preview.strategyComposeLocal')],
              ['docker-remote', t('tasks:preview.strategyDockerRemote')],
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setStrategy(value)}
                className={cn(
                  'px-2 py-0.5 transition-colors',
                  strategy === value ? 'bg-primary text-primary-foreground' : 'hover:bg-muted',
                )}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* DB source chooser — which golden the preview clones from (remote docker only) */}
      {showDeployButton && !isLocal && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">{t('tasks:preview.dbSource')}</span>
          <div className="inline-flex rounded-md border border-border/60 overflow-hidden">
            {([
              ['auto', t('tasks:preview.laneAuto')],
              ['A', t('tasks:preview.laneMain')],
              ['B', t('tasks:preview.laneTest')],
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setLane(value)}
                className={cn(
                  'px-2 py-0.5 transition-colors',
                  lane === value ? 'bg-primary text-primary-foreground' : 'hover:bg-muted',
                )}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex gap-2">
        {showDeployButton && (
          <Button type="button" size="sm" onClick={handleDeploy} disabled={busy} className="flex-1">
            {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Rocket className="mr-2 h-4 w-4" />}
            {status === 'failed' || status === 'stopped' ? t('tasks:preview.redeploy') : t('tasks:preview.deploy')}
          </Button>
        )}

        {isTransient && (
          <Button type="button" size="sm" variant="outline" disabled className="flex-1">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            {statusLabel[status]}
          </Button>
        )}

        {status === 'running' && (
          <>
            {!isLocal && (
              <Button type="button" size="sm" variant="default" onClick={handlePromote} disabled={busy} className="flex-1">
                <ArrowUpCircle className="mr-2 h-4 w-4" />
                {t('tasks:preview.promote')}
              </Button>
            )}
            <Button type="button" size="sm" variant="outline" onClick={handleStop} disabled={busy} className={isLocal ? 'flex-1' : undefined}>
              <Square className="mr-2 h-4 w-4" />
              {t('tasks:preview.stop')}
            </Button>
          </>
        )}

        {isTransient && (
          <Button type="button" size="sm" variant="outline" onClick={handleStop} disabled={busy} title={t('tasks:preview.stop')}>
            <Square className="h-4 w-4" />
          </Button>
        )}
      </div>

      {/* Streamed build/run logs (dev-server & compose-local emit these live) */}
      {logs.length > 0 && (
        <div className="rounded-md border border-border/60 overflow-hidden">
          <button
            type="button"
            onClick={() => setShowLogs((v) => !v)}
            className="flex w-full items-center gap-1.5 px-2 py-1 text-xs font-medium hover:bg-muted/50"
          >
            {showLogs ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
            <Terminal className="h-3.5 w-3.5" />
            {t('tasks:preview.logs')} ({logs.length})
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

export default PreviewDeploy;
