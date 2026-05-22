import { useCallback, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  FileText,
  Folder,
  Loader2,
  RefreshCw,
  Sparkles,
  CheckCircle2,
  AlertCircle,
  Hammer,
} from 'lucide-react';

import { Button } from './ui/button';
import { ScrollArea } from './ui/scroll-area';
import { Separator } from './ui/separator';
import { cn } from '../lib/utils';
import { get, post } from '../lib/api-client';

interface DocsStatus {
  state: 'idle' | 'running';
  has_docs: boolean;
  has_site: boolean;
  last_run?: string;
  last_build?: string;
  last_build_ok?: boolean;
  head_sha?: string;
}

interface DocFile {
  path: string;
  size: number;
}

interface RawDoc {
  path: string;
  content: string;
}

interface DocumentationViewProps {
  projectId: string;
}

const POLL_INTERVAL_MS = 4000;

export function DocumentationView({ projectId }: DocumentationViewProps) {
  const [status, setStatus] = useState<DocsStatus | null>(null);
  const [files, setFiles] = useState<DocFile[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [content, setContent] = useState<string>('');
  const [busy, setBusy] = useState<'generate' | 'build' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    const r = await get<DocsStatus>(`/projects/${projectId}/docs/status`);
    if (r.success && r.data) setStatus(r.data);
  }, [projectId]);

  const loadTree = useCallback(async () => {
    const r = await get<{ files: DocFile[] }>(`/projects/${projectId}/docs/tree`);
    if (r.success && r.data) {
      setFiles(r.data.files);
      if (!selectedPath && r.data.files.length > 0) {
        const homePage =
          r.data.files.find((f) => f.path === 'index.md') ?? r.data.files[0];
        setSelectedPath(homePage.path);
      }
    }
  }, [projectId, selectedPath]);

  const loadContent = useCallback(
    async (path: string) => {
      const r = await get<RawDoc>(
        `/projects/${projectId}/docs/raw?path=${encodeURIComponent(path)}`,
      );
      if (r.success && r.data) {
        setContent(r.data.content);
      } else {
        setContent(`# Could not load ${path}\n\n${r.error ?? ''}`);
      }
    },
    [projectId],
  );

  useEffect(() => {
    loadStatus();
    loadTree();
  }, [loadStatus, loadTree]);

  useEffect(() => {
    if (selectedPath) loadContent(selectedPath);
  }, [selectedPath, loadContent]);

  // Poll while a generation is running; refresh tree once it finishes.
  useEffect(() => {
    if (!status || status.state !== 'running') return;
    const id = setInterval(async () => {
      const r = await get<DocsStatus>(`/projects/${projectId}/docs/status`);
      if (r.success && r.data) {
        const prevState = status.state;
        setStatus(r.data);
        if (prevState === 'running' && r.data.state === 'idle') {
          await loadTree();
          if (selectedPath) await loadContent(selectedPath);
        }
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [status, projectId, loadTree, loadContent, selectedPath]);

  const handleGenerate = useCallback(async () => {
    setBusy('generate');
    setError(null);
    const r = await post<{ state?: string; error?: string }>(
      `/projects/${projectId}/docs/generate`,
    );
    setBusy(null);
    if (!r.success) {
      setError(r.error ?? 'Failed to start generation');
      return;
    }
    await loadStatus();
  }, [projectId, loadStatus]);

  const handleBuild = useCallback(async () => {
    setBusy('build');
    setError(null);
    const r = await post<{ log?: string }>(`/projects/${projectId}/docs/build`);
    setBusy(null);
    if (!r.success) {
      setError(r.error ?? 'Build failed');
    }
    await loadStatus();
    await loadTree();
  }, [projectId, loadStatus, loadTree]);

  const isRunning = status?.state === 'running';

  const treeByFolder = useMemo(() => {
    const groups: Record<string, DocFile[]> = {};
    for (const f of files) {
      const parts = f.path.split('/');
      const folder = parts.length > 1 ? parts.slice(0, -1).join('/') : '';
      (groups[folder] ||= []).push(f);
    }
    return groups;
  }, [files]);

  return (
    <div className="flex h-full">
      {/* Left rail: actions + file tree */}
      <div className="flex w-72 shrink-0 flex-col border-r border-border bg-sidebar/40">
        <div className="p-3 space-y-2">
          <Button
            className="w-full"
            onClick={handleGenerate}
            disabled={isRunning || busy === 'generate'}
          >
            {isRunning || busy === 'generate' ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Generating...
              </>
            ) : (
              <>
                <Sparkles className="mr-2 h-4 w-4" />
                {status?.has_docs ? 'Regenerate docs' : 'Generate docs'}
              </>
            )}
          </Button>
          <Button
            variant="outline"
            className="w-full"
            onClick={handleBuild}
            disabled={isRunning || busy === 'build' || !status?.has_docs}
            title="Rebuild the MkDocs HTML site without re-running the agent"
          >
            {busy === 'build' ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Building...
              </>
            ) : (
              <>
                <Hammer className="mr-2 h-4 w-4" />
                Rebuild site
              </>
            )}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="w-full"
            onClick={() => {
              loadStatus();
              loadTree();
            }}
          >
            <RefreshCw className="mr-2 h-3.5 w-3.5" />
            Refresh
          </Button>
        </div>
        <Separator />
        <div className="px-3 py-2 text-xs text-muted-foreground space-y-1">
          {status?.last_run && (
            <div>
              <span className="font-medium">Last run:</span>{' '}
              {new Date(status.last_run).toLocaleString()}
            </div>
          )}
          {status?.last_build_ok === false && (
            <div className="flex items-center gap-1 text-destructive">
              <AlertCircle className="h-3 w-3" />
              Last build failed
            </div>
          )}
          {status?.last_build_ok === true && (
            <div className="flex items-center gap-1 text-green-600">
              <CheckCircle2 className="h-3 w-3" />
              Site built
            </div>
          )}
        </div>
        <Separator />
        <ScrollArea className="flex-1">
          <div className="p-2 space-y-3">
            {Object.entries(treeByFolder).map(([folder, items]) => (
              <div key={folder || 'root'}>
                {folder && (
                  <div className="flex items-center gap-1 px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    <Folder className="h-3 w-3" />
                    {folder}
                  </div>
                )}
                {items.map((f) => {
                  const label = f.path.split('/').slice(-1)[0];
                  const isActive = selectedPath === f.path;
                  return (
                    <button
                      key={f.path}
                      type="button"
                      onClick={() => setSelectedPath(f.path)}
                      className={cn(
                        'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent/50',
                        isActive && 'bg-accent',
                      )}
                    >
                      <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      <span className="truncate">{label}</span>
                    </button>
                  );
                })}
              </div>
            ))}
            {files.length === 0 && (
              <div className="px-2 py-6 text-center text-xs text-muted-foreground">
                No docs yet. Click <span className="font-medium">Generate docs</span> to
                create them.
              </div>
            )}
          </div>
        </ScrollArea>
      </div>

      {/* Right: rendered markdown */}
      <div className="flex flex-1 flex-col">
        <div className="border-b border-border px-6 py-3 text-xs text-muted-foreground flex items-center justify-between">
          <span>{selectedPath ?? '—'}</span>
          {status?.has_site && (
            <a
              href={`/api/projects/${projectId}/docs/site/index.html`}
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              Open built site ↗
            </a>
          )}
        </div>
        <ScrollArea className="flex-1">
          <div className="prose prose-sm dark:prose-invert max-w-3xl px-6 py-6">
            {error && (
              <div className="not-prose mb-4 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
                {error}
              </div>
            )}
            {selectedPath ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
            ) : (
              <div className="text-muted-foreground">
                Select a file on the left, or generate documentation to start.
              </div>
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  );
}
