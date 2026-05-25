/**
 * "Claude CLI" usage card — surfaces what `claude /usage` shows in the
 * terminal: session / weekly progress bars, today's message count, per-model
 * breakdown from `~/.claude/stats-cache.json`, and a 14-day sparkline.
 *
 * Backed by the existing `POST /api/settings/usage-update` endpoint via
 * `window.API.requestUsageUpdate()`. The header `UsageIndicator` uses the
 * same source — this is the full-detail view.
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Activity, RefreshCw, Terminal } from 'lucide-react';

import { Button } from './ui/button';
import { Card, CardContent, CardHeader, CardTitle } from './ui/card';
import { formatTokens } from '../shared/types/usage';

// Local extension of ClaudeUsageSnapshot. The backend includes these extras
// only on the local-stats path; the header card ignores them.
interface ExtendedSnapshot {
  sessionPercent: number;
  weeklyPercent: number;
  sessionResetTime?: string;
  weeklyResetTime?: string;
  profileName?: string;
  todayMessages?: number;
  weeklyMessages?: number;
  totalOutputTokens?: number;
  totalInputTokens?: number;
  dailyLimit?: number;
  weeklyLimit?: number;
  modelsBreakdown?: Array<{
    model: string;
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
    messageCount: number;
  }>;
  recentDaily?: Array<{ date: string; messageCount: number }>;
}

function barColor(pct: number): string {
  if (pct >= 95) return 'bg-red-500';
  if (pct >= 91) return 'bg-orange-500';
  if (pct >= 71) return 'bg-yellow-500';
  return 'bg-green-500';
}

export function ClaudeCliUsageCard() {
  const { t } = useTranslation(['tasks', 'common']);
  const [snap, setSnap] = useState<ExtendedSnapshot | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchSnap = async () => {
    setLoading(true);
    try {
      const res = await window.API.requestUsageUpdate();
      if (res.success && res.data) {
        setSnap(res.data as ExtendedSnapshot);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSnap();
    // Background updates from the existing onUsageUpdated stream so the card
    // stays current while the user is sitting on the page.
    const unsubscribe = window.API.onUsageUpdated?.((s) => {
      setSnap((prev) => ({ ...(prev ?? {}), ...(s as ExtendedSnapshot) }));
    });
    return () => unsubscribe?.();
  }, []);

  if (!snap) return null;

  const todayMessages = snap.todayMessages ?? 0;
  const weeklyMessages = snap.weeklyMessages ?? 0;
  const dailyLimit = snap.dailyLimit ?? 10000;
  const weeklyLimit = snap.weeklyLimit ?? 70000;
  const breakdown = snap.modelsBreakdown ?? [];
  const recent = snap.recentDaily ?? [];
  const sparklineMax = Math.max(1, ...recent.map((d) => d.messageCount));

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-sm font-semibold flex items-center gap-2">
          <Terminal className="h-4 w-4 text-muted-foreground" />
          {t('tasks:usage.claudeCli.title', 'Claude CLI limits')}
        </CardTitle>
        <Button
          variant="ghost"
          size="sm"
          onClick={fetchSnap}
          disabled={loading}
          className="h-7 px-2"
        >
          <RefreshCw
            className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`}
          />
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Progress bars */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <LimitBar
            label={t('tasks:usage.claudeCli.session', 'Session')}
            percent={snap.sessionPercent}
            count={todayMessages}
            limit={dailyLimit}
            resetLabel={snap.sessionResetTime}
            resetsWord={t('tasks:usage.claudeCli.resets', 'Resets')}
          />
          <LimitBar
            label={t('tasks:usage.claudeCli.weekly', 'Weekly')}
            percent={snap.weeklyPercent}
            count={weeklyMessages}
            limit={weeklyLimit}
            resetLabel={snap.weeklyResetTime}
            resetsWord={t('tasks:usage.claudeCli.resets', 'Resets')}
          />
        </div>

        {/* Headline token totals from stats-cache.json */}
        {(snap.totalInputTokens || snap.totalOutputTokens) ? (
          <div className="grid grid-cols-2 gap-3 text-sm">
            <Stat
              label={t('tasks:usage.input', 'Input')}
              value={formatTokens(snap.totalInputTokens ?? 0)}
            />
            <Stat
              label={t('tasks:usage.output', 'Output')}
              value={formatTokens(snap.totalOutputTokens ?? 0)}
            />
          </div>
        ) : null}

        {/* 14-day activity sparkline */}
        {recent.length > 1 && (
          <div>
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
              {t('tasks:usage.claudeCli.recentActivity', 'Recent activity (msgs/day)')}
            </div>
            <div className="flex items-end gap-1 h-12">
              {recent.map((day) => {
                const h = Math.max(2, (day.messageCount / sparklineMax) * 100);
                return (
                  <div
                    key={day.date}
                    className="flex-1 bg-amber-500/30 hover:bg-amber-500/60 transition-colors rounded-sm"
                    style={{ height: `${h}%` }}
                    title={`${day.date}: ${day.messageCount} msgs`}
                  />
                );
              })}
            </div>
            <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
              <span>{recent[0]?.date}</span>
              <span>{recent[recent.length - 1]?.date}</span>
            </div>
          </div>
        )}

        {/* Per-model breakdown */}
        {breakdown.length > 0 && (
          <div>
            <div className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
              {t('tasks:usage.claudeCli.byModel', 'By model (all-time)')}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="border-b text-muted-foreground">
                  <tr>
                    <th className="text-left font-normal py-1.5 pr-2">
                      {t('tasks:usage.cols.model', 'Model')}
                    </th>
                    <th className="text-right font-normal py-1.5 px-2">
                      {t('tasks:usage.cols.messages', 'Msgs')}
                    </th>
                    <th className="text-right font-normal py-1.5 px-2">
                      {t('tasks:usage.cols.input', 'Input')}
                    </th>
                    <th className="text-right font-normal py-1.5 px-2">
                      {t('tasks:usage.cols.output', 'Output')}
                    </th>
                    <th className="text-right font-normal py-1.5 pl-2">
                      {t('tasks:usage.cols.cacheRead', 'Cache R')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {breakdown.map((m) => (
                    <tr key={m.model} className="border-b last:border-0">
                      <td className="py-1.5 pr-2 font-mono truncate max-w-[160px]" title={m.model}>
                        {m.model}
                      </td>
                      <td className="text-right py-1.5 px-2 tabular-nums">
                        {m.messageCount.toLocaleString()}
                      </td>
                      <td className="text-right py-1.5 px-2 font-mono tabular-nums">
                        {formatTokens(m.inputTokens)}
                      </td>
                      <td className="text-right py-1.5 px-2 font-mono tabular-nums">
                        {formatTokens(m.outputTokens)}
                      </td>
                      <td className="text-right py-1.5 pl-2 font-mono tabular-nums">
                        {formatTokens(m.cacheReadTokens)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground border-t pt-2">
          <Activity className="h-3 w-3" />
          {t(
            'tasks:usage.claudeCli.source',
            'Source: ~/.claude/stats-cache.json — same data as `claude /usage`.',
          )}
          {snap.profileName ? <span className="ml-auto">{snap.profileName}</span> : null}
        </div>
      </CardContent>
    </Card>
  );
}

function LimitBar({
  label,
  percent,
  count,
  limit,
  resetLabel,
  resetsWord,
}: {
  label: string;
  percent: number;
  count: number;
  limit: number;
  resetLabel?: string;
  resetsWord: string;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xs font-medium">{label}</span>
        <span className="text-xs font-mono tabular-nums">
          {count.toLocaleString()} / {limit.toLocaleString()} ·{' '}
          <span className="font-semibold">{Math.round(percent)}%</span>
        </span>
      </div>
      <div className="h-2 rounded-full bg-muted overflow-hidden">
        <div
          className={`h-full transition-all ${barColor(percent)}`}
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
      {resetLabel && (
        <div className="text-[10px] text-muted-foreground mt-1">
          {resetsWord}: {resetLabel}
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-muted/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-0.5 font-mono font-semibold tabular-nums">{value}</div>
    </div>
  );
}
