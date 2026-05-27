import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import Editor from '@monaco-editor/react';
import { Loader2, RotateCcw, Save, Search, Check } from 'lucide-react';

import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { Input } from '../ui/input';
import { cn } from '../../lib/utils';
import { createLogger } from '../../lib/logger';
import type { Project } from '../../shared/types';
import {
  getProjectPrompts,
  getProjectPrompt,
  saveProjectPrompt,
  resetProjectPrompt,
  type PromptCatalogEntry,
  type EffectivePrompt,
} from '../../lib/agent-prompts-api';

const log = createLogger('agent-prompts');

// Order categories sensibly in the list; unknown categories fall to the end.
const CATEGORY_ORDER = [
  'build',
  'spec',
  'qa',
  'ideation',
  'roadmap',
  'github',
  'docs',
  'analysis',
  'mcp_tools',
  'other',
];

interface AgentPromptsSettingsProps {
  project: Project;
}

export function AgentPromptsSettings({ project }: AgentPromptsSettingsProps) {
  const { t } = useTranslation('settings');

  const [entries, setEntries] = useState<PromptCatalogEntry[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [detail, setDetail] = useState<EffectivePrompt | null>(null);
  const [draft, setDraft] = useState('');
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showDefault, setShowDefault] = useState(false);
  const [justSaved, setJustSaved] = useState(false);

  // Detect dark mode for the Monaco theme (matches the app's theme toggle).
  const isDark = typeof document !== 'undefined'
    && document.documentElement.classList.contains('dark');

  const loadList = useCallback(async () => {
    setLoadingList(true);
    setListError(null);
    const res = await getProjectPrompts(project.id);
    if (res.success && res.data) {
      setEntries(res.data);
    } else {
      setListError(res.error || 'Failed to load prompts');
    }
    setLoadingList(false);
  }, [project.id]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  const openPrompt = useCallback(async (key: string) => {
    setSelectedKey(key);
    setShowDefault(false);
    setJustSaved(false);
    setLoadingDetail(true);
    setDetail(null);
    const res = await getProjectPrompt(project.id, key);
    if (res.success && res.data) {
      setDetail(res.data);
      setDraft(res.data.content);
    } else {
      log.error(`Failed to load prompt ${key}`, res.error);
    }
    setLoadingDetail(false);
  }, [project.id]);

  const handleSave = useCallback(async () => {
    if (!selectedKey) return;
    setSaving(true);
    const res = await saveProjectPrompt(project.id, selectedKey, draft);
    if (res.success && res.data) {
      setDetail(res.data);
      setDraft(res.data.content);
      setJustSaved(true);
      setEntries((prev) =>
        prev.map((e) =>
          e.key === selectedKey ? { ...e, isOverridden: true, updatedAt: res.data!.updatedAt } : e,
        ),
      );
    } else {
      log.error(`Failed to save prompt ${selectedKey}`, res.error);
    }
    setSaving(false);
  }, [project.id, selectedKey, draft]);

  const handleReset = useCallback(async () => {
    if (!selectedKey) return;
    setSaving(true);
    const res = await resetProjectPrompt(project.id, selectedKey);
    if (res.success && res.data) {
      setDetail(res.data);
      setDraft(res.data.content);
      setJustSaved(false);
      setEntries((prev) =>
        prev.map((e) =>
          e.key === selectedKey ? { ...e, isOverridden: false, updatedAt: null } : e,
        ),
      );
    } else {
      log.error(`Failed to reset prompt ${selectedKey}`, res.error);
    }
    setSaving(false);
  }, [project.id, selectedKey]);

  const categoryLabel = (cat: string) => t(`agentPrompts.categories.${cat}`, cat);

  // Filter + group entries by category for the list.
  const grouped = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = q
      ? entries.filter(
          (e) => e.displayName.toLowerCase().includes(q) || e.key.toLowerCase().includes(q),
        )
      : entries;
    const byCat = new Map<string, PromptCatalogEntry[]>();
    for (const e of filtered) {
      const arr = byCat.get(e.category) || [];
      arr.push(e);
      byCat.set(e.category, arr);
    }
    return Array.from(byCat.entries()).sort(
      (a, b) => CATEGORY_ORDER.indexOf(a[0]) - CATEGORY_ORDER.indexOf(b[0]),
    );
  }, [entries, search]);

  const dirty = detail !== null && draft !== detail.content;

  return (
    <div className="flex gap-4 h-[60vh] min-h-[420px]">
      {/* Left: prompt list */}
      <div className="w-72 flex-shrink-0 flex flex-col border border-border rounded-lg overflow-hidden">
        <div className="p-2 border-b border-border">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('agentPrompts.searchPlaceholder')}
              className="pl-8 h-9"
            />
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {loadingList ? (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : listError ? (
            <div className="p-4 text-sm text-destructive">{listError}</div>
          ) : (
            grouped.map(([category, items]) => (
              <div key={category}>
                <div className="px-3 pt-3 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {categoryLabel(category)}
                </div>
                {items.map((item) => (
                  <button
                    key={item.key}
                    onClick={() => openPrompt(item.key)}
                    className={cn(
                      'w-full text-left px-3 py-2 flex items-center justify-between gap-2 hover:bg-accent/60 transition-colors',
                      selectedKey === item.key && 'bg-accent',
                    )}
                  >
                    <span className="text-sm truncate">{item.displayName}</span>
                    {item.isOverridden && (
                      <Badge variant="info" className="flex-shrink-0">
                        {t('agentPrompts.customized')}
                      </Badge>
                    )}
                  </button>
                ))}
              </div>
            ))
          )}
        </div>
      </div>

      {/* Right: editor */}
      <div className="flex-1 flex flex-col border border-border rounded-lg overflow-hidden">
        {!selectedKey ? (
          <div className="flex-1 flex items-center justify-center text-sm text-muted-foreground">
            {t('agentPrompts.selectPrompt')}
          </div>
        ) : loadingDetail || !detail ? (
          <div className="flex-1 flex items-center justify-center text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border">
              <div className="min-w-0">
                <div className="text-sm font-medium truncate">{detail.displayName}</div>
                <div className="text-xs text-muted-foreground truncate">{detail.key}</div>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setShowDefault((v) => !v)}
                >
                  {showDefault ? t('agentPrompts.hideDefault') : t('agentPrompts.viewDefault')}
                </Button>
                {detail.isOverridden && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleReset}
                    disabled={saving}
                  >
                    <RotateCcw className="h-3.5 w-3.5 mr-1" />
                    {t('agentPrompts.reset')}
                  </Button>
                )}
                <Button size="sm" onClick={handleSave} disabled={saving || (!dirty && detail.isOverridden)}>
                  {saving ? (
                    <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                  ) : justSaved && !dirty ? (
                    <Check className="h-3.5 w-3.5 mr-1" />
                  ) : (
                    <Save className="h-3.5 w-3.5 mr-1" />
                  )}
                  {t('agentPrompts.save')}
                </Button>
              </div>
            </div>

            {selectedKey === 'coder.md' && (
              <div className="px-3 py-2 text-xs bg-warning/10 text-warning border-b border-border">
                {t('agentPrompts.coderNote')}
              </div>
            )}

            <div className="flex-1 min-h-0">
              <Editor
                height="100%"
                language="markdown"
                theme={isDark ? 'vs-dark' : 'light'}
                value={showDefault ? detail.default : draft}
                onChange={(val) => {
                  if (!showDefault) setDraft(val ?? '');
                }}
                options={{
                  readOnly: showDefault,
                  minimap: { enabled: false },
                  fontSize: 13,
                  lineNumbers: 'on',
                  wordWrap: 'on',
                  automaticLayout: true,
                  scrollBeyondLastLine: false,
                }}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
