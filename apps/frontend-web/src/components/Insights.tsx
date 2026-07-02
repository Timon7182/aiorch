import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  MessageSquare,
  Send,
  Loader2,
  Plus,
  Sparkles,
  User,
  Bot,
  CheckCircle2,
  AlertCircle,
  Search,
  FileText,
  FolderSearch,
  Square,
  ListPlus,
  GitBranch,
  PanelLeft,
  Network,
  Database,
  Paperclip,
  Brain,
  Download,
  RefreshCw,
  X
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Button } from './ui/button';
import { Textarea } from './ui/textarea';
import { ScrollArea } from './ui/scroll-area';
import { Card, CardContent } from './ui/card';
import { Badge } from './ui/badge';
import { cn } from '../lib/utils';
import {
  useInsightsStore,
  loadInsightsSession,
  sendMessage,
  stopMessage,
  newSession,
  switchSession,
  deleteSession,
  renameSession,
  updateModelConfig,
  createTaskFromSuggestion,
  generateTaskFromChat,
  setupInsightsListeners
} from '../stores/insights-store';
import { loadTasks } from '../stores/task-store';
import { useProjectStore, ALL_REPOS } from '../stores/project-store';
import { useIsMobile } from '../hooks/use-media-query';
import { useTranslation } from 'react-i18next';
import { toast } from '../hooks/use-toast';
import { ChatHistorySidebar } from './ChatHistorySidebar';
import { RepoSwitcher } from './RepoSwitcher';
import { CreateTaskFromChatDialog } from './CreateTaskFromChatDialog';
import { InsightsModelSelector } from './InsightsModelSelector';
import { ChatAttachmentBar, processChatFiles, CHAT_FILE_ACCEPT } from './insights/ChatAttachmentBar';
import type { InsightsChatMessage, InsightsModelConfig, InsightsProvider, ChatAttachment, DocsStatus } from '../shared/types';
import {
  TASK_CATEGORY_LABELS,
  TASK_CATEGORY_COLORS,
  TASK_COMPLEXITY_LABELS,
  TASK_COMPLEXITY_COLORS,
  PROVIDER_INFO,
  PROVIDER_MODELS
} from '../shared/constants';

/** Build a model suffix like "(Claude Sonnet 4.6)" or "(Ollama: qwen3-30b)" */
function getModelLabel(provider?: InsightsProvider, model?: string): string | null {
  if (!provider && !model) return null;
  const providerName = provider ? (PROVIDER_INFO[provider]?.displayName || provider) : '';
  if (!model) return providerName || null;

  // Try to find a friendly label from PROVIDER_MODELS
  const models = provider ? PROVIDER_MODELS[provider] : [];
  const match = models?.find((m) => m.id === model);
  if (match) return match.label;

  // Fallback: "Provider: model-id"
  return providerName ? `${providerName}: ${model}` : model;
}

// Safe link renderer for ReactMarkdown to prevent phishing and ensure external links open safely
const SafeLink = ({ href, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
  // Validate URL - only allow http, https, and relative links
  const isValidUrl = href && (
    href.startsWith('http://') ||
    href.startsWith('https://') ||
    href.startsWith('/') ||
    href.startsWith('#')
  );

  if (!isValidUrl) {
    // For invalid or potentially malicious URLs, render as plain text
    return <span className="text-muted-foreground">{children}</span>;
  }

  // External links get security attributes
  const isExternal = href?.startsWith('http://') || href?.startsWith('https://');

  return (
    <a
      href={href}
      {...props}
      {...(isExternal && {
        target: '_blank',
        rel: 'noopener noreferrer',
      })}
      className="text-primary hover:underline"
    >
      {children}
    </a>
  );
};

// Markdown components with safe link rendering
const markdownComponents = {
  a: SafeLink,
};

interface InsightsProps {
  projectId: string;
  onNavigate?: (view: 'kanban' | 'terminals' | 'editor' | 'context' | 'github-issues' | 'github-prs' | 'changelog' | 'insights' | 'worktrees' | 'agent-tools') => void;
}

export function Insights({ projectId, onNavigate }: InsightsProps) {
  const session = useInsightsStore((state) => state.session);
  const sessions = useInsightsStore((state) => state.sessions);
  const status = useInsightsStore((state) => state.status);
  const streamingContent = useInsightsStore((state) => state.streamingContent);
  const streamingThinking = useInsightsStore((state) => state.streamingThinking);
  const currentTool = useInsightsStore((state) => state.currentTool);
  const isLoadingSessions = useInsightsStore((state) => state.isLoadingSessions);
  const lastMetrics = useInsightsStore((state) => state.lastMetrics);

  const { t } = useTranslation(['common']);

  const [inputValue, setInputValue] = useState('');
  // Files/images attached to the next message. Cleared on send.
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const [creatingTask, setCreatingTask] = useState<string | null>(null);
  const [taskCreated, setTaskCreated] = useState<Set<string>>(new Set());
  // On mobile the chat-history panel is hidden behind a toggle so the
  // conversation gets full width; on desktop it stays docked.
  const isMobile = useIsMobile();
  const [historyOpen, setHistoryOpen] = useState(false);

  // The active conversation is reflected in the URL (?session=<id>) so a chat
  // can be linked directly. didApplyUrlSession guards against the reflect
  // effect clobbering an incoming deep-link before it's been applied.
  const [searchParams, setSearchParams] = useSearchParams();
  const sessionParam = searchParams.get('session');
  const didApplyUrlSession = useRef(false);

  // Branch grounding: which branch the chat should read from. '' means the
  // project's current working tree (no worktree); any other value makes the
  // backend answer from a read-only worktree of that branch.
  const projects = useProjectStore((state) => state.projects);
  const projectPath = useMemo(
    () => projects.find((p) => p.id === projectId)?.path ?? null,
    [projects, projectId]
  );
  // Multi-repo projects are a parent folder of child repos (e.g. cts holds
  // backend/ + frontend/). The chat scopes its branch list and grounding to the
  // active child repo; single-repo projects use the project root. Repos are
  // loaded into the store by the sidebar when a project is selected.
  const reposByProject = useProjectStore((state) => state.reposByProject);
  const chatGroundByProject = useProjectStore((state) => state.chatGroundByProject);
  const repos = reposByProject[projectId] ?? [];
  const isMultiRepo = repos.length > 1;
  // Chat grounding: a multi-repo project defaults to ALL_REPOS (the whole
  // project root) so the assistant can read/search every repo + the docs and
  // decide where to look; picking a specific repo narrows it. activeRepoPath is
  // null in all-repos mode, which makes the send/codegraph logic below pass
  // repo=undefined → the backend grounds the chat in the project root.
  const chatGround = isMultiRepo ? (chatGroundByProject[projectId] ?? ALL_REPOS) : null;
  const activeRepoPath = isMultiRepo
    ? (chatGround === ALL_REPOS ? null : chatGround)
    : projectPath;
  const [branches, setBranches] = useState<string[]>([]);
  const [currentBranch, setCurrentBranch] = useState<string>('');
  const [selectedBranch, setSelectedBranch] = useState<string>('');
  // Whether CodeGraph is indexed for the dir the chat will run against
  // (the selected branch's worktree / active repo). Drives whether the model
  // selector offers the CodeGraph option. Optimistic default avoids flicker.
  const [cgcAvailable, setCgcAvailable] = useState<boolean>(true);
  // Whether a graphify graph.json exists for the dir the chat runs against.
  const [graphifyAvailable, setGraphifyAvailable] = useState<boolean>(false);
  // Documentation freshness for that dir (drives the "docs outdated" banner).
  const [docsStatus, setDocsStatus] = useState<DocsStatus | null>(null);
  const [docsBannerDismissed, setDocsBannerDismissed] = useState<boolean>(false);
  const [refreshingDocs, setRefreshingDocs] = useState<boolean>(false);

  // Create Task from Chat state
  const [showCreateTaskDialog, setShowCreateTaskDialog] = useState(false);
  const [isGeneratingTask, setIsGeneratingTask] = useState(false);
  const [isCreatingGeneratedTask, setIsCreatingGeneratedTask] = useState(false);
  const [generatedTitle, setGeneratedTitle] = useState('');
  const [generatedDescription, setGeneratedDescription] = useState('');

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Load session and set up listeners on mount
  useEffect(() => {
    didApplyUrlSession.current = false;
    loadInsightsSession(projectId);
    const cleanup = setupInsightsListeners();
    return cleanup;
  }, [projectId]);

  // Apply a ?session= deep link once the session list has loaded.
  useEffect(() => {
    if (didApplyUrlSession.current) return;
    if (!sessionParam) { didApplyUrlSession.current = true; return; }
    if (sessions.length === 0) return; // wait for sessions to load
    if (session?.id === sessionParam) { didApplyUrlSession.current = true; return; }
    if (sessions.some((s) => s.id === sessionParam)) {
      switchSession(projectId, sessionParam);
    }
    didApplyUrlSession.current = true;
  }, [sessionParam, sessions, session?.id, projectId]);

  // Reflect the active conversation back into the URL.
  useEffect(() => {
    if (!didApplyUrlSession.current) return;
    const current = searchParams.get('session');
    if (session?.id && session.id !== current) {
      const next = new URLSearchParams(searchParams);
      next.set('session', session.id);
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id]);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [session?.messages, streamingContent, streamingThinking]);

  // Focus textarea on mount
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  // Reset taskCreated when switching sessions
  useEffect(() => {
    setTaskCreated(new Set());
  }, [session?.id]);

  // Load available branches + current branch so the user can ground the chat
  // in a branch other than the one currently checked out.
  useEffect(() => {
    if (!activeRepoPath) {
      setBranches([]);
      setCurrentBranch('');
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const [branchesResult, currentResult] = await Promise.all([
          window.API.getGitBranches(activeRepoPath),
          window.API.getCurrentGitBranch(activeRepoPath),
        ]);
        if (cancelled) return;
        setBranches(branchesResult.success && branchesResult.data ? branchesResult.data : []);
        setCurrentBranch(currentResult.success && currentResult.data ? currentResult.data : '');
      } catch (err) {
        console.error('Failed to load branches for chat:', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeRepoPath]);

  // Reset branch choice back to the working tree when the project or active
  // repo changes (each repo has its own branches).
  useEffect(() => {
    setSelectedBranch('');
  }, [activeRepoPath]);

  // CodeGraph is only offered when the selected branch/repo's working dir is
  // actually indexed (a fresh branch worktree has no .codegraphcontext/). Fetch
  // availability whenever that scope changes so the selector can disable it.
  useEffect(() => {
    let cancelled = false;
    const branch = selectedBranch || undefined;
    const repo = isMultiRepo && activeRepoPath ? activeRepoPath : undefined;
    window.API.getInsightsCodeSearchAvailability(projectId, branch, repo)
      .then((res) => {
        if (cancelled) return;
        const data = res.success ? res.data : undefined;
        setCgcAvailable(data ? data.cgc : false);
        setGraphifyAvailable(data ? data.graphify : false);
        setDocsStatus(data?.docs ?? null);
        setDocsBannerDismissed(false);
      })
      .catch(() => {
        if (cancelled) return;
        setCgcAvailable(false);
        setGraphifyAvailable(false);
        setDocsStatus(null);
      });
    return () => { cancelled = true; };
  }, [projectId, selectedBranch, activeRepoPath, isMultiRepo]);

  // Kick off a code-graph / docs re-index for the current repo scope, then
  // dismiss the "docs outdated" banner (freshness will re-fetch on next scope
  // change or reload).
  const handleRefreshDocs = useCallback(async () => {
    setRefreshingDocs(true);
    try {
      await window.API.refreshInsightsCodegraph(projectId, activeRepoPath ?? undefined);
      setDocsBannerDismissed(true);
    } catch (err) {
      console.error('Failed to refresh docs/code-graph:', err);
    } finally {
      setRefreshingDocs(false);
    }
  }, [projectId, activeRepoPath]);

  const handleSend = () => {
    const message = inputValue.trim();
    // Allow sending with attachments only (no typed text required).
    if ((!message && attachments.length === 0) || status.phase === 'thinking' || status.phase === 'streaming') return;

    setInputValue('');
    const sentAttachments = attachments;
    setAttachments([]);
    setAttachmentError(null);
    // Only pass a branch when it differs from the current checkout — otherwise
    // the backend would needlessly build a worktree of the branch we're on.
    const branch = selectedBranch && selectedBranch !== currentBranch ? selectedBranch : undefined;
    // For multi-repo projects, scope grounding to the active child repo.
    const repo = isMultiRepo && activeRepoPath ? activeRepoPath : undefined;
    sendMessage(
      projectId,
      message,
      sentAttachments.length > 0 ? sentAttachments : undefined,
      session?.modelConfig,
      branch,
      repo,
    );
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Add picked/pasted/dropped files as attachments. Shared by the file picker,
  // paste handler, and drop handler so they all enforce the same caps/types.
  const handleAddFiles = async (files: FileList | File[]) => {
    if (isLoading) return;
    const { attachments: added, errors } = await processChatFiles(files, attachments);
    if (added.length > 0) setAttachments((prev) => [...prev, ...added]);
    setAttachmentError(errors.length > 0 ? errors.join(' ') : null);
  };

  // Capture pasted images. Do NOT preventDefault — let the browser paste text
  // naturally so mixed text+image pastes keep both (mirrors TaskCreationWizard).
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const imageFiles = Array.from(e.clipboardData.items)
      .filter((item) => item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter((f): f is File => f !== null);
    if (imageFiles.length > 0) handleAddFiles(imageFiles);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDraggingOver(false);
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) handleAddFiles(files);
  };

  const handleRemoveAttachment = (id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
    setAttachmentError(null);
  };

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) handleAddFiles(files);
    if (fileInputRef.current) fileInputRef.current.value = ''; // allow re-picking same file
  };

  const handleNewSession = async () => {
    await newSession(projectId);
    setTaskCreated(new Set());
    textareaRef.current?.focus();
  };

  const handleSelectSession = async (sessionId: string) => {
    if (sessionId !== session?.id) {
      await switchSession(projectId, sessionId);
    }
  };

  const handleDeleteSession = async (sessionId: string): Promise<boolean> => {
    return await deleteSession(projectId, sessionId);
  };

  const handleRenameSession = async (sessionId: string, newTitle: string): Promise<boolean> => {
    return await renameSession(projectId, sessionId, newTitle);
  };

  const handleCreateTask = async (message: InsightsChatMessage) => {
    if (!message.suggestedTask) return;

    setCreatingTask(message.id);
    try {
      const task = await createTaskFromSuggestion(
        projectId,
        message.suggestedTask.title,
        message.suggestedTask.description,
        message.suggestedTask.metadata
      );

      if (task) {
        setTaskCreated(prev => new Set(prev).add(message.id));
        // Reload tasks to show the new task in the kanban
        loadTasks(projectId);
      }
    } finally {
      setCreatingTask(null);
    }
  };

  const handleModelConfigChange = async (config: InsightsModelConfig) => {
    // If we have a session, persist the config
    if (session?.id) {
      await updateModelConfig(projectId, session.id, config);
    }
  };

  const handleGenerateTask = async () => {
    setShowCreateTaskDialog(true);
    setIsGeneratingTask(true);
    setGeneratedTitle('');
    setGeneratedDescription('');

    try {
      const result = await generateTaskFromChat(projectId, session?.modelConfig);
      if (result) {
        setGeneratedTitle(result.title);
        setGeneratedDescription(result.description);
      }
    } finally {
      setIsGeneratingTask(false);
    }
  };

  const handleConfirmGeneratedTask = async (title: string, description: string) => {
    setIsCreatingGeneratedTask(true);
    try {
      const task = await createTaskFromSuggestion(projectId, title, description);
      if (task) {
        loadTasks(projectId);
        setShowCreateTaskDialog(false);
        onNavigate?.('kanban');
      }
    } finally {
      setIsCreatingGeneratedTask(false);
    }
  };

  const isLoading = status.phase === 'thinking' || status.phase === 'streaming';
  const messages = session?.messages || [];

  return (
    <div className="flex h-full">
      {/* Mobile backdrop for the history drawer */}
      {isMobile && (
        <div
          className={cn(
            'fixed inset-0 z-30 bg-black/60 backdrop-blur-sm transition-opacity duration-300 md:hidden',
            historyOpen ? 'opacity-100' : 'pointer-events-none opacity-0'
          )}
          onClick={() => setHistoryOpen(false)}
          aria-hidden="true"
        />
      )}
      {/* Chat History Sidebar — docked on desktop, slide-in drawer on mobile
          so the conversation can use the full width on small screens. */}
      <div
        className={cn(
          'shrink-0',
          isMobile &&
            'fixed inset-y-0 left-0 z-40 transition-transform duration-300 ease-in-out will-change-transform',
          isMobile && (historyOpen ? 'translate-x-0' : '-translate-x-full')
        )}
      >
        <ChatHistorySidebar
          sessions={sessions}
          currentSessionId={session?.id || null}
          isLoading={isLoadingSessions}
          onNewSession={() => {
            handleNewSession();
            if (isMobile) setHistoryOpen(false);
          }}
          onSelectSession={(id) => {
            handleSelectSession(id);
            if (isMobile) setHistoryOpen(false);
          }}
          onDeleteSession={handleDeleteSession}
          onRenameSession={handleRenameSession}
        />
      </div>

      {/* Main Chat Area */}
      <div className="flex flex-1 flex-col min-w-0">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-4 py-3 md:px-6 md:py-4">
          <div className="flex items-center gap-2 md:gap-3 min-w-0">
            {isMobile && (
              <Button
                variant="ghost"
                size="icon"
                className="h-9 w-9 shrink-0"
                onClick={() => setHistoryOpen(true)}
                aria-label="Chat history"
              >
                <PanelLeft className="h-5 w-5" />
              </Button>
            )}
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary/10">
              <Sparkles className="h-5 w-5 text-primary" />
            </div>
            <div className="min-w-0">
              <h2 className="font-semibold text-foreground">Chat</h2>
              <p className="hidden truncate text-sm text-muted-foreground sm:block">
                Ask questions about your codebase
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {/* Repo picker — only renders for multi-repo projects (e.g. cts).
                Scopes the branch list + chat grounding to the chosen repo. */}
            <RepoSwitcher projectId={projectId} />
            <InsightsModelSelector
              projectId={projectId}
              currentConfig={session?.modelConfig}
              onConfigChange={handleModelConfigChange}
              disabled={isLoading}
              cgcAvailable={cgcAvailable}
              graphifyAvailable={graphifyAvailable}
            />
            {messages.length > 0 && !isLoading && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleGenerateTask}
              >
                <ListPlus className="mr-2 h-4 w-4" />
                {t('common:insights.createTask.button')}
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={handleNewSession}
            >
              <Plus className="mr-2 h-4 w-4" />
              New Chat
            </Button>
          </div>
        </div>

      {/* Stale documentation banner — docs exist but code moved past them. */}
      {docsStatus?.hasDocs && !docsStatus.fresh && docsStatus.docsSha && !docsBannerDismissed && (
        <div className="flex items-center gap-3 border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm md:px-6">
          <AlertCircle className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <span className="min-w-0 flex-1 text-amber-800 dark:text-amber-200">
            {t('common:insights.docsBanner.message', 'Documentation is outdated — it may not reflect the latest code.')}
          </span>
          <Button
            variant="outline"
            size="sm"
            className="h-7 shrink-0"
            onClick={handleRefreshDocs}
            disabled={refreshingDocs}
          >
            {refreshingDocs
              ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
              : <RefreshCw className="mr-1.5 h-3.5 w-3.5" />}
            {t('common:insights.docsBanner.refresh', 'Refresh')}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            onClick={() => setDocsBannerDismissed(true)}
            aria-label={t('common:insights.docsBanner.dismiss', 'Dismiss')}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      )}

      {/* Messages */}
      <ScrollArea className="flex-1 px-6 py-4">
        {messages.length === 0 && !streamingContent ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
              <MessageSquare className="h-8 w-8 text-muted-foreground" />
            </div>
            <h3 className="mb-2 text-lg font-medium text-foreground">
              Start a Conversation
            </h3>
            <p className="max-w-md text-sm text-muted-foreground">
              Ask questions about your codebase, get suggestions for improvements,
              or discuss features you'd like to implement.
            </p>
            <Button
              variant="outline"
              size="default"
              className="mt-6 text-sm font-medium"
              onClick={() => {
                setInputValue("Let's create a new task together");
                textareaRef.current?.focus();
              }}
            >
              <ListPlus className="mr-2 h-4 w-4" />
              Let's create a new task together
            </Button>
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              {[
                'What is the architecture of this project?',
                'Suggest improvements for code quality',
                'What features could I add next?',
                'Are there any security concerns?'
              ].map((suggestion) => (
                <Button
                  key={suggestion}
                  variant="outline"
                  size="sm"
                  className="text-xs"
                  onClick={() => {
                    setInputValue(suggestion);
                    textareaRef.current?.focus();
                  }}
                >
                  {suggestion}
                </Button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-6">
            {messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                projectPath={projectPath}
                onCreateTask={() => handleCreateTask(message)}
                isCreatingTask={creatingTask === message.id}
                taskCreated={taskCreated.has(message.id)}
              />
            ))}

            {/* Streaming message */}
            {(streamingContent || streamingThinking || currentTool) && (
              <div className="flex gap-3">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10">
                  <Bot className="h-4 w-4 text-primary" />
                </div>
                <div className="flex-1">
                  <div className="mb-1 text-sm font-medium text-foreground">
                    Assistant{(() => { const m = getModelLabel(session?.modelConfig?.provider, session?.modelConfig?.model); return m ? <span className="font-normal text-muted-foreground"> ({m})</span> : null; })()}
                  </div>
                  {/* Live extended-thinking trace — shown only while the turn is
                      streaming. Cleared once the assistant message finalizes. */}
                  {streamingThinking && (
                    <div className="mb-2 rounded-md border border-border/60 bg-muted/40 px-3 py-2 text-xs">
                      <div className="mb-1 flex items-center gap-1.5 font-medium text-muted-foreground">
                        <Brain className="h-3.5 w-3.5" />
                        <span>Reasoning</span>
                        {!streamingContent && (
                          <Loader2 className="h-3 w-3 animate-spin opacity-70" />
                        )}
                      </div>
                      <div className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-muted-foreground/90">
                        {streamingThinking}
                      </div>
                    </div>
                  )}
                  {streamingContent && (
                    <div className="prose prose-sm dark:prose-invert max-w-none">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        rehypePlugins={[rehypeHighlight]}
                        components={markdownComponents}
                      >
                        {streamingContent}
                      </ReactMarkdown>
                    </div>
                  )}
                  {/* Tool usage indicator */}
                  {currentTool && (
                    <ToolIndicator name={currentTool.name} input={currentTool.input} />
                  )}
                </div>
              </div>
            )}

            {/* Metrics badge — shown after response completes */}
            {lastMetrics && status.phase === 'complete' && (
              <div className="flex justify-end">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-muted/50 px-2.5 py-1 text-[11px] text-muted-foreground">
                  {lastMetrics.tokensPerSecond > 0 && (
                    <span className="font-medium">{lastMetrics.tokensPerSecond} tok/s</span>
                  )}
                  {lastMetrics.outputTokens > 0 && (
                    <span>{lastMetrics.estimated ? '~' : ''}{lastMetrics.outputTokens} tokens</span>
                  )}
                  <span>{lastMetrics.elapsedSeconds}s</span>
                </span>
              </div>
            )}

            {/* Thinking indicator — initial wait before any reasoning/text arrives */}
            {status.phase === 'thinking' && !streamingContent && !streamingThinking && !currentTool && (
              <div className="flex gap-3">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10">
                  <Bot className="h-4 w-4 text-primary" />
                </div>
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Thinking...
                </div>
              </div>
            )}

            {/* Error message */}
            {status.phase === 'error' && status.error && (
              <div className="flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
                <AlertCircle className="h-4 w-4 shrink-0" />
                {status.error}
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        )}
      </ScrollArea>

      {/* Input */}
      <div
        className={cn(
          'border-t border-border p-4 transition-colors',
          isDraggingOver && 'bg-primary/5'
        )}
        onDragOver={(e) => { e.preventDefault(); if (!isLoading) setIsDraggingOver(true); }}
        onDragLeave={(e) => { e.preventDefault(); setIsDraggingOver(false); }}
        onDrop={handleDrop}
      >
        {branches.length > 0 && (
          <div className="mb-2 flex items-center gap-2">
            <GitBranch className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <select
              value={selectedBranch}
              onChange={(e) => setSelectedBranch(e.target.value)}
              disabled={isLoading}
              title="Read from a specific branch (a read-only worktree; your working tree is not touched)"
              className="h-7 rounded-md border border-input bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
            >
              <option value="">
                {currentBranch ? `Current branch (${currentBranch})` : 'Current working tree'}
              </option>
              {branches
                .filter((b) => b !== currentBranch)
                .map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
            </select>
            {selectedBranch && selectedBranch !== currentBranch && (
              <span className="text-xs text-muted-foreground">
                reading from <code className="font-mono">{selectedBranch}</code> (read-only)
              </span>
            )}
          </div>
        )}
        {/* Pending attachments (above the input) */}
        <ChatAttachmentBar
          attachments={attachments}
          onRemove={handleRemoveAttachment}
          error={attachmentError}
          className="mb-2"
        />
        <input
          ref={fileInputRef}
          type="file"
          accept={CHAT_FILE_ACCEPT}
          multiple
          onChange={handleFileInputChange}
          className="hidden"
        />
        <div className="flex gap-2">
          <Textarea
            ref={textareaRef}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder="Ask about your codebase... (attach images or text files)"
            className="min-h-[80px] resize-none"
            disabled={isLoading}
          />
          <div className="flex flex-col gap-2 self-end">
            <Button
              variant="outline"
              size="icon"
              onClick={() => fileInputRef.current?.click()}
              disabled={isLoading}
              title="Attach images or text/code files"
            >
              <Paperclip className="h-4 w-4" />
            </Button>
            {isLoading ? (
              <Button
                variant="destructive"
                onClick={() => stopMessage(projectId)}
                title="Stop response"
              >
                <Square className="h-4 w-4" />
              </Button>
            ) : (
              <Button
                onClick={handleSend}
                disabled={!inputValue.trim() && attachments.length === 0}
              >
                <Send className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          Press Enter to send, Shift+Enter for new line
        </p>
      </div>
      </div>

      {/* Create Task from Chat Dialog */}
      <CreateTaskFromChatDialog
        open={showCreateTaskDialog}
        onOpenChange={setShowCreateTaskDialog}
        initialTitle={generatedTitle}
        initialDescription={generatedDescription}
        isGenerating={isGeneratingTask}
        onConfirm={handleConfirmGeneratedTask}
        isCreating={isCreatingGeneratedTask}
      />
    </div>
  );
}

/**
 * Pull file paths the assistant mentions out of its message text so we can
 * offer one-click downloads. Matches tokens with at least one `/` that end in a
 * file extension (e.g. `.magestic-ai/specs/003-x/CHECKLIST.md`), optionally
 * wrapped in backticks/quotes/parens. URLs and extension-less API routes (like
 * `/v1/permissions/role-permissions-matrix`) are skipped. Deduped, capped at 12.
 */
function extractDownloadableFiles(content: string): string[] {
  if (!content) return [];
  const out = new Set<string>();
  const re = /(?:^|[\s`("'])((?:\.{1,2}\/|\/)?(?:[\w.@-]+\/)+[\w.@-]+\.[A-Za-z0-9]{1,8})/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    const p = m[1].replace(/[.,;:)\]'"`]+$/, '');
    if (/^[a-z][\w+.-]*:\/\//i.test(p) || p.startsWith('//')) continue; // skip URLs
    out.add(p);
    if (out.size >= 12) break;
  }
  return Array.from(out);
}

/**
 * Download chips shown under an assistant message for any files it references.
 * Resolves relative paths against the project root and pulls content through the
 * existing `/files/read` endpoint, then triggers a browser download.
 */
function MessageFileDownloads({
  projectPath,
  content,
}: {
  projectPath: string | null;
  content: string;
}) {
  const { t } = useTranslation(['common']);
  const [busy, setBusy] = useState<string | null>(null);
  const files = useMemo(() => extractDownloadableFiles(content), [content]);
  if (files.length === 0) return null;

  const handleDownload = async (rel: string) => {
    const name = rel.split('/').pop() || 'file';
    const abs = rel.startsWith('/')
      ? rel
      : projectPath
        ? `${projectPath.replace(/\/+$/, '')}/${rel}`
        : rel;
    setBusy(rel);
    try {
      const res = await window.API.readFile(abs);
      if (!res.success || typeof res.data?.content !== 'string') {
        toast({ variant: 'destructive', title: t('common:chatFiles.downloadFailed', { name }) });
        return;
      }
      const url = URL.createObjectURL(
        new Blob([res.data.content], { type: 'text/plain;charset=utf-8' })
      );
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      toast({ variant: 'destructive', title: t('common:chatFiles.downloadFailed', { name }) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5 pt-1">
      <span className="text-xs text-muted-foreground">{t('common:chatFiles.heading')}:</span>
      {files.map((f) => {
        const name = f.split('/').pop() || f;
        return (
          <Button
            key={f}
            variant="outline"
            size="sm"
            className="h-6 gap-1 px-2 text-xs font-normal"
            disabled={busy === f}
            title={t('common:chatFiles.download', { name })}
            onClick={() => handleDownload(f)}
          >
            {busy === f ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Download className="h-3 w-3" />
            )}
            <span className="max-w-[180px] truncate">{name}</span>
          </Button>
        );
      })}
    </div>
  );
}

interface MessageBubbleProps {
  message: InsightsChatMessage;
  projectPath: string | null;
  onCreateTask: () => void;
  isCreatingTask: boolean;
  taskCreated: boolean;
}

function MessageBubble({
  message,
  projectPath,
  onCreateTask,
  isCreatingTask,
  taskCreated
}: MessageBubbleProps) {
  const isUser = message.role === 'user';

  return (
    <div className="flex gap-3">
      <div
        className={cn(
          'flex h-8 w-8 shrink-0 items-center justify-center rounded-full',
          isUser ? 'bg-muted' : 'bg-primary/10'
        )}
      >
        {isUser ? (
          <User className="h-4 w-4 text-muted-foreground" />
        ) : (
          <Bot className="h-4 w-4 text-primary" />
        )}
      </div>
      <div className="flex-1 space-y-2">
        <div className="text-sm font-medium text-foreground">
          {isUser ? 'You' : <>Assistant{(() => { const m = getModelLabel(message.provider, message.providerModel); return m ? <span className="font-normal text-muted-foreground"> ({m})</span> : null; })()}</>}
        </div>
        {message.content && (
          <div className="prose prose-sm dark:prose-invert max-w-none">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
              components={markdownComponents}
            >
              {message.content}
            </ReactMarkdown>
          </div>
        )}

        {/* Download chips for files the assistant references on disk */}
        {!isUser && message.content && (
          <MessageFileDownloads projectPath={projectPath} content={message.content} />
        )}

        {/* Attachments sent with this message (read-only) */}
        {message.attachments && message.attachments.length > 0 && (
          <ChatAttachmentBar attachments={message.attachments} />
        )}

        {/* Tool usage history for assistant messages */}
        {!isUser && message.toolsUsed && message.toolsUsed.length > 0 && (
          <ToolUsageHistory tools={message.toolsUsed} />
        )}

        {/* Task suggestion card */}
        {message.suggestedTask && (
          <Card className="mt-3 border-primary/20 bg-primary/5">
            <CardContent className="p-4">
              <div className="mb-2 flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-primary" />
                <span className="text-sm font-medium text-primary">
                  Suggested Task
                </span>
              </div>
              <h4 className="mb-2 font-medium text-foreground">
                {message.suggestedTask.title}
              </h4>
              <p className="mb-3 text-sm text-muted-foreground">
                {message.suggestedTask.description}
              </p>
              {message.suggestedTask.metadata && (
                <div className="mb-3 flex flex-wrap gap-2">
                  {message.suggestedTask.metadata.category && (
                    <Badge
                      variant="outline"
                      className={cn(
                        'text-xs',
                        TASK_CATEGORY_COLORS[message.suggestedTask.metadata.category]
                      )}
                    >
                      {TASK_CATEGORY_LABELS[message.suggestedTask.metadata.category] ||
                        message.suggestedTask.metadata.category}
                    </Badge>
                  )}
                  {message.suggestedTask.metadata.complexity && (
                    <Badge
                      variant="outline"
                      className={cn(
                        'text-xs',
                        TASK_COMPLEXITY_COLORS[message.suggestedTask.metadata.complexity]
                      )}
                    >
                      {TASK_COMPLEXITY_LABELS[message.suggestedTask.metadata.complexity] ||
                        message.suggestedTask.metadata.complexity}
                    </Badge>
                  )}
                </div>
              )}
              <Button
                size="sm"
                onClick={onCreateTask}
                disabled={isCreatingTask || taskCreated}
              >
                {isCreatingTask ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Creating...
                  </>
                ) : taskCreated ? (
                  <>
                    <CheckCircle2 className="mr-2 h-4 w-4" />
                    Task Created
                  </>
                ) : (
                  <>
                    <Plus className="mr-2 h-4 w-4" />
                    Create Task
                  </>
                )}
              </Button>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

// Tool usage history component for showing tools used in completed messages
interface ToolUsageHistoryProps {
  tools: Array<{
    name: string;
    input?: string;
    result?: string;
    isError?: boolean;
    timestamp: Date;
  }>;
}

// `mcp__db__query` -> `db: query` so MCP steps read cleanly in the history.
function prettyToolName(name: string): string {
  if (name.startsWith('mcp__')) {
    const parts = name.split('__');
    if (parts.length >= 3) return `${parts[1]}: ${parts.slice(2).join('__')}`;
  }
  return name;
}

function getToolIcon(toolName: string) {
  if (toolName.startsWith('mcp__db__')) return Database;
  if (toolName.startsWith('mcp__')) return Network; // codegraph + any other MCP
  switch (toolName) {
    case 'Read':
      return FileText;
    case 'Glob':
      return FolderSearch;
    case 'Grep':
      return Search;
    default:
      return FileText;
  }
}

function getToolColor(toolName: string) {
  if (toolName.startsWith('mcp__db__')) return 'text-cyan-500';
  if (toolName.startsWith('mcp__')) return 'text-purple-500';
  switch (toolName) {
    case 'Read':
      return 'text-blue-500';
    case 'Glob':
      return 'text-amber-500';
    case 'Grep':
      return 'text-green-500';
    default:
      return 'text-muted-foreground';
  }
}

function ToolUsageHistory({ tools }: ToolUsageHistoryProps) {
  // Expanded by default so the agent's tool/MCP steps (e.g. the SQL it ran and
  // what came back) are visible without an extra click.
  const [expanded, setExpanded] = useState(true);

  if (tools.length === 0) return null;

  // Group tools by name for summary
  const toolCounts = tools.reduce((acc, tool) => {
    acc[tool.name] = (acc[tool.name] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);

  return (
    <div className="mt-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <span className="flex items-center gap-1">
          {Object.entries(toolCounts).map(([name, count]) => {
            const Icon = getToolIcon(name);
            return (
              <span key={name} className={cn('flex items-center gap-0.5', getToolColor(name))}>
                <Icon className="h-3 w-3" />
                <span>{count}</span>
              </span>
            );
          })}
        </span>
        <span>{tools.length} tool{tools.length !== 1 ? 's' : ''} used</span>
        <span className="text-[10px]">{expanded ? '▲' : '▼'}</span>
      </button>

      {expanded && (
        <div className="mt-2 space-y-2 rounded-md border border-border bg-muted/30 p-2">
          {tools.map((tool, index) => {
            const Icon = getToolIcon(tool.name);
            return (
              <div key={`${tool.name}-${index}`} className="text-xs">
                <div className="flex items-center gap-2">
                  <Icon className={cn('h-3 w-3 shrink-0', getToolColor(tool.name))} />
                  <span className="font-medium">{prettyToolName(tool.name)}</span>
                  {tool.isError && (
                    <span className="text-[10px] font-medium text-red-500">error</span>
                  )}
                </div>
                {tool.input && (
                  <pre className="mt-1 ml-5 overflow-x-auto whitespace-pre-wrap break-words rounded bg-background/60 px-2 py-1 font-mono text-[11px] text-muted-foreground">
                    {tool.input}
                  </pre>
                )}
                {tool.result && (
                  <pre
                    className={cn(
                      'mt-1 ml-5 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-background/60 px-2 py-1 font-mono text-[11px]',
                      tool.isError ? 'text-red-500' : 'text-muted-foreground'
                    )}
                  >
                    {tool.result}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Tool indicator component for showing what the AI is currently doing
interface ToolIndicatorProps {
  name: string;
  input?: string;
}

function ToolIndicator({ name, input }: ToolIndicatorProps) {
  // Get friendly name and icon for each tool
  const getToolInfo = (toolName: string) => {
    if (toolName.startsWith('mcp__codegraph__')) {
      return {
        icon: Network,
        label: 'Querying code graph',
        color: 'text-purple-500 bg-purple-500/10'
      };
    }
    if (toolName.startsWith('mcp__db__')) {
      return {
        icon: Database,
        label: 'Querying database',
        color: 'text-cyan-500 bg-cyan-500/10'
      };
    }
    if (toolName.startsWith('mcp__')) {
      return {
        icon: Network,
        label: prettyToolName(toolName),
        color: 'text-purple-500 bg-purple-500/10'
      };
    }
    switch (toolName) {
      case 'Read':
        return {
          icon: FileText,
          label: 'Reading file',
          color: 'text-blue-500 bg-blue-500/10'
        };
      case 'Glob':
        return {
          icon: FolderSearch,
          label: 'Searching files',
          color: 'text-amber-500 bg-amber-500/10'
        };
      case 'Grep':
        return {
          icon: Search,
          label: 'Searching code',
          color: 'text-green-500 bg-green-500/10'
        };
      default:
        return {
          icon: Loader2,
          label: toolName,
          color: 'text-primary bg-primary/10'
        };
    }
  };

  const { icon: Icon, label, color } = getToolInfo(name);

  return (
    <div className={cn(
      'mt-2 inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm',
      color
    )}>
      <Icon className="h-4 w-4 animate-pulse" />
      <span className="font-medium">{label}</span>
      {input && (
        <span className="text-muted-foreground truncate max-w-[300px]">
          {input}
        </span>
      )}
    </div>
  );
}
