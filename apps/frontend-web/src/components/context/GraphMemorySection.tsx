import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  RefreshCw,
  Search,
  Plus,
  Trash2,
  Network,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  Clock
} from 'lucide-react';
import { Button } from '../ui/button';
import { Card, CardContent } from '../ui/card';
import { Badge } from '../ui/badge';
import { Input } from '../ui/input';
import { Textarea } from '../ui/textarea';
import { cn } from '../../lib/utils';
import { useContextStore } from '../../stores/context-store';
import { useGraphMemory } from './hooks';
import { formatDate } from './utils';
import type { GraphMemoryEpisode, GraphMemoryKind } from '../../shared/types';

interface GraphMemorySectionProps {
  projectId: string;
}

const KINDS: GraphMemoryKind[] = ['fact', 'pattern', 'gotcha'];

function EpisodeRow({
  episode,
  onDelete
}: {
  episode: GraphMemoryEpisode;
  onDelete: (uuid: string) => void;
}) {
  const { t } = useTranslation('context');
  const [expanded, setExpanded] = useState(false);
  const content = episode.content || '';
  const isLong = content.length > 160;
  const preview = isLong && !expanded ? `${content.slice(0, 160)}…` : content;
  const when = episode.valid_at || episode.created_at;

  return (
    <Card className="bg-muted/30 border-border/50 hover:border-border transition-colors">
      <CardContent className="pt-4 pb-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            {episode.name && (
              <Badge variant="outline" className="text-xs font-mono mb-2 max-w-full truncate">
                {episode.name}
              </Badge>
            )}
            <p className="text-sm text-foreground whitespace-pre-wrap break-words">
              {preview}
            </p>
            <div className="flex items-center gap-2 mt-2 text-xs text-muted-foreground">
              <Clock className="h-3 w-3" />
              {when ? formatDate(when) : ''}
              {episode.source_description && (
                <span className="truncate max-w-[240px]" title={episode.source_description}>
                  · {episode.source_description}
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {isLong && (
              <Button variant="ghost" size="sm" onClick={() => setExpanded(!expanded)} className="gap-1">
                {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                {expanded ? t('graph.collapse') : t('graph.expand')}
              </Button>
            )}
            {episode.uuid && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onDelete(episode.uuid as string)}
                className="text-destructive hover:text-destructive"
                title={t('graph.delete')}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function GraphMemorySection({ projectId }: GraphMemorySectionProps) {
  const { t } = useTranslation('context');
  const {
    graphEpisodes,
    graphLoading,
    graphAvailable,
    graphReason,
    graphSearchResults,
    graphSearchLoading,
    graphSearchQuery
  } = useContextStore();
  const { reload, search, add, remove } = useGraphMemory(projectId);

  const [query, setQuery] = useState('');
  const [content, setContent] = useState('');
  const [kind, setKind] = useState<GraphMemoryKind>('fact');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = () => {
    if (query.trim()) search(query);
  };

  const handleAdd = async () => {
    if (!content.trim() || saving) return;
    setSaving(true);
    setError(null);
    const err = await add(content.trim(), kind);
    setSaving(false);
    if (err) {
      setError(err);
    } else {
      setContent('');
    }
  };

  const handleDelete = async (uuid: string) => {
    // eslint-disable-next-line no-alert
    if (!window.confirm(t('graph.confirmDelete'))) return;
    const err = await remove(uuid);
    if (err) setError(err);
  };

  const kindLabel = (k: GraphMemoryKind) =>
    k === 'pattern' ? t('graph.kindPattern') : k === 'gotcha' ? t('graph.kindGotcha') : t('graph.kindFact');

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
            <Network className="h-4 w-4" />
            {t('graph.title')}
          </h3>
          <p className="text-xs text-muted-foreground mt-0.5">{t('graph.subtitle')}</p>
        </div>
        <Button variant="ghost" size="sm" onClick={() => reload()} title={t('graph.reload')}>
          <RefreshCw className={cn('h-4 w-4', graphLoading && 'animate-spin')} />
        </Button>
      </div>

      {!graphAvailable && !graphLoading && graphReason && (
        <div className="flex items-start gap-3 p-4 rounded-lg bg-muted/50 text-muted-foreground border border-border">
          <AlertCircle className="h-5 w-5 shrink-0 mt-0.5" />
          <p className="text-sm">{graphReason}</p>
        </div>
      )}

      {graphAvailable && (
        <>
          {/* Add memory form */}
          <div className="space-y-2 rounded-lg border border-border p-3">
            <label className="text-xs font-medium text-muted-foreground">{t('graph.addTitle')}</label>
            <Textarea
              placeholder={t('graph.contentPlaceholder')}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={3}
            />
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-1">
                {KINDS.map((k) => (
                  <Button
                    key={k}
                    type="button"
                    variant={kind === k ? 'default' : 'outline'}
                    size="sm"
                    onClick={() => setKind(k)}
                  >
                    {kindLabel(k)}
                  </Button>
                ))}
              </div>
              <Button onClick={handleAdd} disabled={!content.trim() || saving} className="gap-1">
                <Plus className="h-4 w-4" />
                {saving ? t('graph.saving') : t('graph.save')}
              </Button>
            </div>
          </div>

          {error && (
            <div className="flex items-center gap-3 p-3 rounded-lg bg-destructive/10 text-destructive border border-destructive/30">
              <AlertCircle className="h-4 w-4 shrink-0" />
              <p className="text-sm">{error}</p>
            </div>
          )}

          {/* Search */}
          <div className="flex gap-2">
            <Input
              placeholder={t('graph.searchPlaceholder')}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            />
            <Button onClick={handleSearch} disabled={graphSearchLoading}>
              <Search className={cn('h-4 w-4', graphSearchLoading && 'animate-pulse')} />
            </Button>
          </div>

          {graphSearchQuery && graphSearchResults.length === 0 && !graphSearchLoading && (
            <div className="flex items-center gap-3 p-4 rounded-lg bg-muted/50 text-muted-foreground border border-border">
              <AlertCircle className="h-5 w-5 shrink-0" />
              <p className="text-sm">{t('graph.noResults')}</p>
            </div>
          )}

          {graphSearchResults.length > 0 && (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">
                {t('graph.results', { count: graphSearchResults.length })}
              </p>
              {graphSearchResults.map((r, idx) => (
                <Card key={idx} className="bg-muted/30 border-border/50">
                  <CardContent className="pt-3 pb-3">
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-sm text-foreground whitespace-pre-wrap break-words flex-1">
                        {r.content}
                      </p>
                      <Badge variant="secondary" className="text-xs shrink-0">
                        {r.score?.toFixed(2) ?? '0'}
                      </Badge>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}

          {/* Episode list */}
          {graphLoading && graphEpisodes.length === 0 ? (
            <div className="flex items-center justify-center py-6">
              <RefreshCw className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : graphEpisodes.length === 0 ? (
            <p className="text-sm text-muted-foreground py-2">{t('graph.empty')}</p>
          ) : (
            <div className="space-y-2">
              {graphEpisodes.map((ep, idx) => (
                <EpisodeRow key={ep.uuid || idx} episode={ep} onDelete={handleDelete} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
