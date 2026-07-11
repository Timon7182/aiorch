import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  RefreshCw,
  Database,
  Brain,
  Search,
  CheckCircle,
  XCircle,
  AlertCircle
} from 'lucide-react';
import { Button } from '../ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/card';
import { Badge } from '../ui/badge';
import { Input } from '../ui/input';
import { ScrollArea } from '../ui/scroll-area';
import { cn } from '../../lib/utils';
import { MemoryCard } from './MemoryCard';
import { InfoItem } from './InfoItem';
import { GraphMemorySection } from './GraphMemorySection';
import type { GraphitiMemoryStatus, GraphitiMemoryState, MemoryEpisode } from '../../shared/types';

interface MemoriesTabProps {
  projectId: string;
  memoryStatus: GraphitiMemoryStatus | null;
  memoryState: GraphitiMemoryState | null;
  recentMemories: MemoryEpisode[];
  memoriesLoading: boolean;
  searchResults: Array<{ type: string; content: string; score: number }>;
  searchLoading: boolean;
  searchQuery: string;
  onSearch: (query: string) => void;
}

export function MemoriesTab({
  projectId,
  memoryStatus,
  memoryState,
  recentMemories,
  memoriesLoading,
  searchResults,
  searchLoading,
  searchQuery,
  onSearch
}: MemoriesTabProps) {
  const { t } = useTranslation('context');
  const [localSearchQuery, setLocalSearchQuery] = useState('');

  const handleSearch = () => {
    if (localSearchQuery.trim()) {
      onSearch(localSearchQuery);
    }
  };

  const handleSearchKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleSearch();
    }
  };

  // The graph status card is driven by the real graph state when available,
  // falling back to the file-based availability flag.
  const graphConnected = memoryState?.enabled ?? memoryStatus?.available ?? false;
  const providerLabel = memoryState?.embedderProvider
    ? memoryState.embedderProvider.charAt(0).toUpperCase() + memoryState.embedderProvider.slice(1)
    : '—';

  return (
    <ScrollArea className="h-full">
      <div className="p-6 space-y-6">
        {/* Graph Memory Status */}
        <Card>
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base flex items-center gap-2">
                <Database className="h-4 w-4" />
                {t('status.title')}
              </CardTitle>
              {graphConnected ? (
                <Badge variant="outline" className="bg-success/10 text-success border-success/30">
                  <CheckCircle className="h-3 w-3 mr-1" />
                  {t('status.connected')}
                </Badge>
              ) : (
                <Badge variant="outline" className="bg-muted text-muted-foreground">
                  <XCircle className="h-3 w-3 mr-1" />
                  {t('status.notAvailable')}
                </Badge>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {graphConnected ? (
              <>
                <div className="grid gap-3 sm:grid-cols-3 text-sm">
                  <InfoItem label={t('status.provider')} value={providerLabel} />
                  <InfoItem label={t('status.database')} value={memoryState?.database || 'magestic_ai_memory'} />
                  <InfoItem
                    label={t('status.episodes')}
                    value={(memoryState?.episode_count ?? 0).toString()}
                  />
                  {memoryState?.groupId && (
                    <InfoItem label={t('status.groupId')} value={memoryState.groupId} />
                  )}
                  <InfoItem
                    label={t('status.apiKey')}
                    value={memoryState?.hasApiKey ? t('status.apiKeySet') : t('status.apiKeyMissing')}
                  />
                </div>
                {memoryState?.last_session != null && (
                  <p className="text-xs text-muted-foreground">
                    {t('status.lastSession', { number: memoryState.last_session })}
                  </p>
                )}
              </>
            ) : (
              <div className="text-sm text-muted-foreground">
                <p>{memoryStatus?.reason || t('status.notConfigured')}</p>
                <p className="mt-2 text-xs">{t('status.disabledHint')}</p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Graph Memory (editable knowledge graph) */}
        <GraphMemorySection projectId={projectId} />

        {/* ---- Session Insights (file-based build session records) ---- */}
        <div className="pt-2 border-t border-border">
          <div className="mt-4">
            <h3 className="text-sm font-semibold text-foreground">{t('sessionInsights.title')}</h3>
            <p className="text-xs text-muted-foreground mt-0.5">{t('sessionInsights.subtitle')}</p>
          </div>
        </div>

        {/* Search */}
        <div className="space-y-4">
          <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
            {t('sessionInsights.searchTitle')}
          </h3>
          <div className="flex gap-2">
            <Input
              placeholder={t('sessionInsights.searchPlaceholder')}
              value={localSearchQuery}
              onChange={(e) => setLocalSearchQuery(e.target.value)}
              onKeyDown={handleSearchKeyDown}
            />
            <Button onClick={handleSearch} disabled={searchLoading}>
              <Search className={cn('h-4 w-4', searchLoading && 'animate-pulse')} />
            </Button>
          </div>

          {/* No Results Alert */}
          {searchQuery && searchResults.length === 0 && !searchLoading && (
            <div className="flex items-center gap-3 p-4 rounded-lg bg-muted/50 text-muted-foreground border border-border">
              <AlertCircle className="h-5 w-5 shrink-0" />
              <p className="text-sm">{t('sessionInsights.noResults')}</p>
            </div>
          )}

          {/* Search Results */}
          {searchResults.length > 0 && (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">
                {t('sessionInsights.resultsFound', { count: searchResults.length })}
              </p>
              {searchResults.map((result, idx) => {
                // Transform search result to MemoryEpisode format for MemoryCard
                const memoryData: MemoryEpisode = {
                  id: (result as any).id || `search-${idx}`,
                  type: (result.type as MemoryEpisode['type']) || 'session_insight',
                  timestamp: (result as any).timestamp || new Date().toISOString(),
                  session_number: (result as any).sessionNumber,
                  // Store full data as JSON in content for MemoryCard to parse
                  content: JSON.stringify({
                    spec_id: (result as any).specId,
                    session_number: (result as any).sessionNumber,
                    subtasks_completed: (result as any).subtasksCompleted || [],
                    what_worked: (result as any).whatWorked || [],
                    what_failed: (result as any).whatFailed || [],
                    recommendations_for_next_session: (result as any).recommendations || [],
                    discoveries: (result as any).discoveries || {}
                  })
                };
                return (
                  <div key={idx} className="relative">
                    <Badge
                      variant="secondary"
                      className="absolute -top-2 right-2 z-10 text-xs"
                    >
                      Score: {result.score?.toFixed(1) || '0'}
                    </Badge>
                    <MemoryCard memory={memoryData} />
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Recent Session Insights */}
        <div className="space-y-4">
          <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
            {t('sessionInsights.recentTitle')}
          </h3>

          {memoriesLoading && (
            <div className="flex items-center justify-center py-8">
              <RefreshCw className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          )}

          {!memoriesLoading && recentMemories.length === 0 && (
            <div className="flex flex-col items-center justify-center py-8 text-center">
              <Brain className="h-10 w-10 text-muted-foreground mb-3" />
              <p className="text-sm text-muted-foreground">
                {t('sessionInsights.empty')}
              </p>
            </div>
          )}

          {recentMemories.length > 0 && (
            <div className="space-y-3">
              {recentMemories.map((memory) => (
                <MemoryCard key={memory.id} memory={memory} />
              ))}
            </div>
          )}
        </div>
      </div>
    </ScrollArea>
  );
}
