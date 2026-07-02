import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
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
  Square,
  RotateCw,
  Network,
  ExternalLink,
  GitBranch,
  Webhook,
} from 'lucide-react';

import { Button } from './ui/button';
import { ScrollArea } from './ui/scroll-area';
import { Separator } from './ui/separator';
import { RepoSwitcher } from './RepoSwitcher';
import { cn } from '../lib/utils';
import { get, post } from '../lib/api-client';
import { getAuthToken } from '../lib/auth';
import { useProjectStore } from '../stores/project-store';

interface DocsStatus {
  state: 'idle' | 'running';
  has_docs: boolean;
  has_site: boolean;
  has_graph?: boolean;
  has_codegraph?: boolean;
  codegraph_indexing?: boolean;
  last_run?: string;
  last_build?: string;
  last_build_ok?: boolean;
  last_graphify?: string;
  last_codegraph?: string;
  head_sha?: string;
  branch?: string | null;
}

// Sentinel paths used to signal "show the graphify / CodeGraphContext report
// in the right pane" instead of one of the docs/*.md files. Anything starting
// with __ is reserved and won't collide with a real markdown filename.
const GRAPH_REPORT_PATH = '__graph_report__';
const CODEGRAPH_REPORT_PATH = '__codegraph_report__';

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
  const { t } = useTranslation('docs');
  const [status, setStatus] = useState<DocsStatus | null>(null);
  const [files, setFiles] = useState<DocFile[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [content, setContent] = useState<string>('');
  const [busy, setBusy] = useState<
    'generate' | 'build' | 'stop' | 'restart' | 'codegraph' | 'hook' | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  // Optimistic flag: set true the instant the user clicks Generate/Restart,
  // cleared once the server confirms (status=running) or the request fails.
  // Closes the small window between the POST returning and the subprocess
  // appearing in /status.
  const [optimisticRunning, setOptimisticRunning] = useState(false);

  // For multi-repo projects, scope docs to the active child repo. The query
  // suffix is appended to every docs request so the backend reads/writes the
  // right repo's docs/, graphify-out/, and docs-site/.
  const reposByProject = useProjectStore((s) => s.reposByProject);
  const activeRepoByProject = useProjectStore((s) => s.activeRepoByProject);
  const repos = reposByProject[projectId] ?? [];
  const activeRepoPath = repos.length > 1
    ? (activeRepoByProject[projectId] ?? repos[0]?.path)
    : undefined;

  // Branch-aware docs: view/generate docs from a branch other than the current
  // checkout. '' = current checkout (fully backward compatible). The repo path
  // used for branch listing works for single- and multi-repo projects.
  const [branches, setBranches] = useState<string[]>([]);
  const [currentBranch, setCurrentBranch] = useState<string>('');
  const [selectedBranch, setSelectedBranch] = useState<string>('');
  const [hookMsg, setHookMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const branchRepoPath = activeRepoByProject[projectId] ?? repos[0]?.path;
  // Only send a branch param when it differs from the current checkout.
  const branchParam = selectedBranch && selectedBranch !== currentBranch ? selectedBranch : undefined;

  // Append the repo (multi-repo scope) and branch params to every docs request
  // so the backend reads/writes the right repo + branch worktree.
  const repoSuffix = (hasQuery: boolean) => {
    const parts: string[] = [];
    if (activeRepoPath) parts.push(`repo=${encodeURIComponent(activeRepoPath)}`);
    if (branchParam) parts.push(`branch=${encodeURIComponent(branchParam)}`);
    if (parts.length === 0) return '';
    return `${hasQuery ? '&' : '?'}${parts.join('&')}`;
  };

  const loadStatus = useCallback(async () => {
    const r = await get<DocsStatus>(`/projects/${projectId}/docs/status${repoSuffix(false)}`);
    if (r.success && r.data) setStatus(r.data);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRepoPath, branchParam]);

  // Selection lives independently of the tree fetch (see the selection effect
  // below), so loadTree only fetches — it must NOT depend on selectedPath or
  // it would refetch the whole tree on every file click.
  const loadTree = useCallback(async () => {
    const r = await get<{ files: DocFile[] }>(`/projects/${projectId}/docs/tree${repoSuffix(false)}`);
    if (r.success && r.data) setFiles(r.data.files);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRepoPath, branchParam]);

  // Monotonic token so a slow/404 content response for a no-longer-selected
  // file (e.g. the previous repo's file during a repo switch) can't clobber
  // the content we've since loaded for the current selection.
  const contentReqRef = useRef(0);

  const loadContent = useCallback(
    async (path: string) => {
      const reqId = ++contentReqRef.current;
      // Graph report lives in graphify-out/, not docs/, so it has its
      // own endpoint. Same response shape, so we slot it into the same
      // markdown viewer.
      let url: string;
      if (path === GRAPH_REPORT_PATH) {
        url = `/projects/${projectId}/docs/graph-report${repoSuffix(false)}`;
      } else if (path === CODEGRAPH_REPORT_PATH) {
        url = `/projects/${projectId}/docs/codegraph-report${repoSuffix(false)}`;
      } else {
        url = `/projects/${projectId}/docs/raw?path=${encodeURIComponent(path)}${repoSuffix(true)}`;
      }
      const r = await get<RawDoc>(url);
      if (reqId !== contentReqRef.current) return; // a newer load superseded us
      if (r.success && r.data) {
        setContent(r.data.content);
      } else {
        setContent(`# Could not load ${path}\n\n${r.error ?? ''}`);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [projectId, activeRepoPath, branchParam],
  );

  useEffect(() => {
    loadStatus();
    loadTree();
  }, [loadStatus, loadTree]);

  // Load the repo's branches + current branch so docs can be viewed/generated
  // from a branch other than the checkout. Reuses the same git endpoints the
  // Insights chat branch selector uses.
  useEffect(() => {
    if (!branchRepoPath) {
      setBranches([]);
      setCurrentBranch('');
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const [branchesResult, currentResult] = await Promise.all([
          window.API.getGitBranches(branchRepoPath),
          window.API.getCurrentGitBranch(branchRepoPath),
        ]);
        if (cancelled) return;
        setBranches(branchesResult.success && branchesResult.data ? branchesResult.data : []);
        setCurrentBranch(currentResult.success && currentResult.data ? currentResult.data : '');
      } catch {
        if (!cancelled) {
          setBranches([]);
          setCurrentBranch('');
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [branchRepoPath]);

  // Reset the branch choice back to the checkout when the project or repo
  // changes (each repo has its own branches).
  useEffect(() => {
    setSelectedBranch('');
  }, [projectId, branchRepoPath]);

  useEffect(() => {
    if (selectedPath) loadContent(selectedPath);
  }, [selectedPath, loadContent]);

  // Keep the selection valid for the tree currently loaded. Runs on first load
  // (auto-select the home page) and whenever the tree changes underneath us —
  // notably after a repo switch (or the repo store hydrating), where the
  // previously-selected file belongs to a different repo and would 404. The
  // graph-report sentinel isn't a docs/ file, so it's exempt.
  useEffect(() => {
    if (selectedPath === GRAPH_REPORT_PATH || selectedPath === CODEGRAPH_REPORT_PATH) return;
    if (files.length === 0) {
      if (selectedPath) {
        setSelectedPath(null);
        setContent('');
      }
      return;
    }
    if (!selectedPath || !files.some((f) => f.path === selectedPath)) {
      const home = files.find((f) => f.path === 'index.md') ?? files[0];
      setSelectedPath(home.path);
    }
  }, [files, selectedPath]);

  // Poll while a generation OR a code-graph index is running; refresh the
  // tree once generation finishes (the CGC panel reacts to has_codegraph on
  // its own, so indexing just needs the status to keep updating).
  useEffect(() => {
    if (!status || (status.state !== 'running' && !status.codegraph_indexing)) return;
    const id = setInterval(async () => {
      const r = await get<DocsStatus>(`/projects/${projectId}/docs/status${repoSuffix(false)}`);
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, projectId, activeRepoPath, branchParam, loadTree, loadContent, selectedPath]);

  const handleGenerate = useCallback(async () => {
    setBusy('generate');
    setError(null);
    setOptimisticRunning(true);
    const r = await post<{ state?: string; error?: string }>(
      `/projects/${projectId}/docs/generate${repoSuffix(false)}`,
    );
    setBusy(null);
    if (!r.success) {
      setOptimisticRunning(false);
      setError(r.error ?? 'Failed to start generation');
      return;
    }
    await loadStatus();
    setOptimisticRunning(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRepoPath, branchParam, loadStatus]);

  const handleBuild = useCallback(async () => {
    setBusy('build');
    setError(null);
    const r = await post<{ log?: string }>(`/projects/${projectId}/docs/build${repoSuffix(false)}`);
    setBusy(null);
    if (!r.success) {
      setError(r.error ?? 'Build failed');
    }
    await loadStatus();
    await loadTree();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRepoPath, branchParam, loadStatus, loadTree]);

  const handleIndexCodegraph = useCallback(async () => {
    setBusy('codegraph');
    setError(null);
    const r = await post<{ state?: string; error?: string }>(
      `/projects/${projectId}/docs/codegraph/index${repoSuffix(false)}`,
    );
    setBusy(null);
    if (!r.success) {
      setError(r.error ?? 'Failed to start code-graph indexing');
    }
    await loadStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRepoPath, branchParam, loadStatus]);

  const handleStop = useCallback(async () => {
    setBusy('stop');
    setError(null);
    const r = await post<{ state?: string; error?: string }>(
      `/projects/${projectId}/docs/cancel`,
    );
    setBusy(null);
    setOptimisticRunning(false);
    if (!r.success) {
      setError(r.error ?? 'Failed to stop generation');
    }
    await loadStatus();
  }, [projectId, loadStatus]);

  const handleRestart = useCallback(async () => {
    setBusy('restart');
    setError(null);
    await post(`/projects/${projectId}/docs/cancel`);
    setOptimisticRunning(true);
    const r = await post<{ state?: string; error?: string }>(
      `/projects/${projectId}/docs/generate${repoSuffix(false)}`,
    );
    setBusy(null);
    if (!r.success) {
      setOptimisticRunning(false);
      setError(r.error ?? 'Failed to restart generation');
      return;
    }
    await loadStatus();
    setOptimisticRunning(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRepoPath, branchParam, loadStatus]);

  // Install the post-commit hook that requests a docs refresh on external
  // commits (honored by the optional watcher). Toasts done/error.
  const handleInstallHook = useCallback(async () => {
    setBusy('hook');
    setError(null);
    setHookMsg(null);
    const r = await post<{ state?: string; error?: string }>(
      `/projects/${projectId}/docs/install-hook${repoSuffix(false)}`,
    );
    setBusy(null);
    if (!r.success) {
      setHookMsg({ ok: false, text: r.error ?? t('hook.error') });
    } else {
      setHookMsg({ ok: true, text: t('hook.installed') });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, activeRepoPath, branchParam, t]);

  const isRunning = status?.state === 'running' || optimisticRunning;

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
      {/* Left rail: actions + file tree — narrows on small screens */}
      <div className="flex w-48 sm:w-60 md:w-72 shrink-0 flex-col border-r border-border bg-sidebar/40">
        <div className="p-3 space-y-2">
          <RepoSwitcher projectId={projectId} className="w-full justify-between" />
          {branches.length > 0 && (
            <div className="flex items-center gap-2">
              <GitBranch className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <select
                value={selectedBranch}
                onChange={(e) => setSelectedBranch(e.target.value)}
                disabled={isRunning}
                title={t('branch.hint')}
                className="h-8 flex-1 rounded-md border border-input bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
              >
                <option value="">
                  {currentBranch
                    ? t('branch.current', { branch: currentBranch })
                    : t('branch.checkedOut')}
                </option>
                {branches
                  .filter((b) => b !== currentBranch)
                  .map((b) => (
                    <option key={b} value={b}>
                      {b}
                    </option>
                  ))}
              </select>
            </div>
          )}
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
          {isRunning && (
            <div className="grid grid-cols-2 gap-2">
              <Button
                variant="destructive"
                size="sm"
                onClick={handleStop}
                disabled={busy === 'stop' || busy === 'restart'}
                title="Cancel the running generation"
              >
                {busy === 'stop' ? (
                  <>
                    <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                    Stopping...
                  </>
                ) : (
                  <>
                    <Square className="mr-2 h-3.5 w-3.5" />
                    Stop
                  </>
                )}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={handleRestart}
                disabled={busy === 'restart' || busy === 'stop'}
                title="Cancel and start a new generation"
              >
                {busy === 'restart' ? (
                  <>
                    <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                    Restarting...
                  </>
                ) : (
                  <>
                    <RotateCw className="mr-2 h-3.5 w-3.5" />
                    Restart
                  </>
                )}
              </Button>
            </div>
          )}
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
            variant="outline"
            className="w-full"
            onClick={handleIndexCodegraph}
            disabled={isRunning || busy === 'codegraph' || status?.codegraph_indexing}
            title="Build/refresh the CodeGraphContext code index — powers caller/callee, dead-code and complexity tools for the agents"
          >
            {busy === 'codegraph' || status?.codegraph_indexing ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Indexing code...
              </>
            ) : (
              <>
                <Network className="mr-2 h-4 w-4" />
                {status?.has_codegraph ? 'Refresh code graph' : 'Build code graph'}
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
          <Button
            variant="outline"
            size="sm"
            className="w-full"
            onClick={handleInstallHook}
            disabled={busy === 'hook'}
            title={t('hook.hint')}
          >
            {busy === 'hook' ? (
              <>
                <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
                {t('hook.installing')}
              </>
            ) : (
              <>
                <Webhook className="mr-2 h-3.5 w-3.5" />
                {t('hook.install')}
              </>
            )}
          </Button>
          {hookMsg && (
            <div
              className={cn(
                'flex items-center gap-1 text-xs',
                hookMsg.ok ? 'text-green-600' : 'text-destructive',
              )}
            >
              {hookMsg.ok ? (
                <CheckCircle2 className="h-3 w-3 shrink-0" />
              ) : (
                <AlertCircle className="h-3 w-3 shrink-0" />
              )}
              <span className="truncate" title={hookMsg.text}>{hookMsg.text}</span>
            </div>
          )}
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
          {status?.has_graph && status?.last_graphify && (
            <div>
              <span className="font-medium">Graph:</span>{' '}
              {new Date(status.last_graphify).toLocaleString()}
            </div>
          )}
          {status?.has_codegraph && status?.last_codegraph && (
            <div>
              <span className="font-medium">Code graph:</span>{' '}
              {new Date(status.last_codegraph).toLocaleString()}
            </div>
          )}
        </div>
        {status?.has_graph && (
          <>
            <Separator />
            <div className="px-3 py-2 space-y-1">
              <div className="flex items-center gap-1 px-1 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                <Network className="h-3 w-3" />
                Knowledge graph
              </div>
              <button
                type="button"
                onClick={() => setSelectedPath(GRAPH_REPORT_PATH)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent/50',
                  selectedPath === GRAPH_REPORT_PATH && 'bg-accent',
                )}
                title="Human-readable summary: god nodes, surprising links, suggested questions"
              >
                <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="truncate">GRAPH_REPORT.md</span>
              </button>
              <a
                href={`/api/projects/${projectId}/docs/graph/graph.html?token=${encodeURIComponent(getAuthToken() ?? '')}${repoSuffix(true)}`}
                target="_blank"
                rel="noreferrer"
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm text-primary hover:bg-accent/50"
                title="Open the interactive node-and-edge browser"
              >
                <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">Interactive graph ↗</span>
              </a>
              <a
                href={`/api/projects/${projectId}/docs/graph/graph.json?token=${encodeURIComponent(getAuthToken() ?? '')}${repoSuffix(true)}`}
                target="_blank"
                rel="noreferrer"
                className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-muted-foreground hover:bg-accent/50"
                title="Raw graph data"
              >
                <FileText className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">graph.json</span>
              </a>
            </div>
          </>
        )}
        {status?.has_codegraph && (
          <>
            <Separator />
            <div className="px-3 py-2 space-y-1">
              <div className="flex items-center gap-1 px-1 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                <Network className="h-3 w-3" />
                Code graph (CGC)
              </div>
              <button
                type="button"
                onClick={() => setSelectedPath(CODEGRAPH_REPORT_PATH)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent/50',
                  selectedPath === CODEGRAPH_REPORT_PATH && 'bg-accent',
                )}
                title="CodeGraphContext: god nodes, complexity hotspots, cross-module links, suggested queries"
              >
                <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="truncate">CGC_REPORT.md</span>
              </button>
            </div>
          </>
        )}
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
      <div className="flex flex-1 flex-col min-w-0">
        <div className="border-b border-border px-6 py-3 text-xs text-muted-foreground flex items-center justify-between">
          <span>
            {selectedPath === GRAPH_REPORT_PATH
              ? 'GRAPH_REPORT.md (graphify)'
              : selectedPath === CODEGRAPH_REPORT_PATH
                ? 'CGC_REPORT.md (CodeGraphContext)'
                : selectedPath ?? '—'}
          </span>
          {status?.has_site && (
            <a
              href={`/api/projects/${projectId}/docs/site/index.html?token=${encodeURIComponent(getAuthToken() ?? '')}${repoSuffix(true)}`}
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
