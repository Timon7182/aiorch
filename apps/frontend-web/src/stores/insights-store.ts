import { create } from 'zustand';
import type {
  InsightsSession,
  InsightsSessionSummary,
  InsightsChatMessage,
  InsightsChatStatus,
  InsightsStreamChunk,
  InsightsStreamMetrics,
  InsightsToolUsage,
  InsightsModelConfig,
  InsightsProviderInfo,
  ChatAttachment,
  TaskMetadata,
  Task
} from '../shared/types';
import { DEFAULT_FEATURE_MODELS, DEFAULT_FEATURE_THINKING } from '../shared/constants/models';

interface ToolUsage {
  name: string;
  input?: string;
}

interface InsightsState {
  // Data
  session: InsightsSession | null;
  sessions: InsightsSessionSummary[]; // List of all sessions
  status: InsightsChatStatus;
  pendingMessage: string;
  streamingContent: string; // Accumulates streaming response
  streamingThinking: string; // Accumulates live extended-thinking deltas for the current turn
  currentTool: ToolUsage | null; // Currently executing tool
  toolsUsed: InsightsToolUsage[]; // Tools used during current response
  isLoadingSessions: boolean;
  availableProviders: InsightsProviderInfo[];
  isLoadingProviders: boolean;
  lastMetrics: InsightsStreamMetrics | null; // Metrics from the last completed response

  // Actions
  setSession: (session: InsightsSession | null) => void;
  setSessions: (sessions: InsightsSessionSummary[]) => void;
  setStatus: (status: InsightsChatStatus) => void;
  setPendingMessage: (message: string) => void;
  addMessage: (message: InsightsChatMessage) => void;
  updateLastAssistantMessage: (content: string) => void;
  appendStreamingContent: (content: string) => void;
  clearStreamingContent: () => void;
  appendStreamingThinking: (content: string) => void;
  clearStreamingThinking: () => void;
  setCurrentTool: (tool: ToolUsage | null) => void;
  addToolUsage: (tool: ToolUsage) => void;
  updateLastTool: (patch: Partial<InsightsToolUsage>) => void;
  clearToolsUsed: () => void;
  setLastMetrics: (metrics: InsightsStreamMetrics | null) => void;
  finalizeStreamingMessage: (suggestedTask?: InsightsChatMessage['suggestedTask']) => void;
  clearSession: () => void;
  setLoadingSessions: (loading: boolean) => void;
  setAvailableProviders: (providers: InsightsProviderInfo[]) => void;
  setLoadingProviders: (loading: boolean) => void;
}

const initialStatus: InsightsChatStatus = {
  phase: 'idle',
  message: ''
};

export const useInsightsStore = create<InsightsState>((set, _get) => ({
  // Initial state
  session: null,
  sessions: [],
  status: initialStatus,
  pendingMessage: '',
  streamingContent: '',
  streamingThinking: '',
  currentTool: null,
  toolsUsed: [],
  isLoadingSessions: false,
  availableProviders: [],
  isLoadingProviders: false,
  lastMetrics: null,

  // Actions
  setSession: (session) => set({ session }),

  setSessions: (sessions) => set({ sessions }),

  setStatus: (status) => set({ status }),

  setLoadingSessions: (loading) => set({ isLoadingSessions: loading }),

  setAvailableProviders: (providers) => set({ availableProviders: providers }),

  setLoadingProviders: (loading) => set({ isLoadingProviders: loading }),

  setPendingMessage: (message) => set({ pendingMessage: message }),

  addMessage: (message) =>
    set((state) => {
      if (!state.session) {
        // Create new session if none exists
        return {
          session: {
            id: `session-${Date.now()}`,
            projectId: '',
            messages: [message],
            createdAt: new Date(),
            updatedAt: new Date()
          }
        };
      }

      return {
        session: {
          ...state.session,
          messages: [...state.session.messages, message],
          updatedAt: new Date()
        }
      };
    }),

  updateLastAssistantMessage: (content) =>
    set((state) => {
      if (!state.session || state.session.messages.length === 0) return state;

      const messages = [...state.session.messages];
      const lastIndex = messages.length - 1;
      const lastMessage = messages[lastIndex];

      if (lastMessage.role === 'assistant') {
        messages[lastIndex] = { ...lastMessage, content };
      }

      return {
        session: {
          ...state.session,
          messages,
          updatedAt: new Date()
        }
      };
    }),

  appendStreamingContent: (content) =>
    set((state) => ({
      streamingContent: state.streamingContent + content
    })),

  clearStreamingContent: () => set({ streamingContent: '' }),

  appendStreamingThinking: (content) =>
    set((state) => ({
      streamingThinking: state.streamingThinking + content
    })),

  clearStreamingThinking: () => set({ streamingThinking: '' }),

  setCurrentTool: (tool) => set({ currentTool: tool }),

  addToolUsage: (tool) =>
    set((state) => ({
      toolsUsed: [
        ...state.toolsUsed,
        {
          name: tool.name,
          input: tool.input,
          timestamp: new Date()
        }
      ]
    })),

  updateLastTool: (patch) =>
    set((state) => {
      if (state.toolsUsed.length === 0) return {};
      const toolsUsed = [...state.toolsUsed];
      toolsUsed[toolsUsed.length - 1] = {
        ...toolsUsed[toolsUsed.length - 1],
        ...patch,
      };
      return { toolsUsed };
    }),

  clearToolsUsed: () => set({ toolsUsed: [] }),

  setLastMetrics: (metrics) => set({ lastMetrics: metrics }),

  finalizeStreamingMessage: (suggestedTask) =>
    set((state) => {
      const content = state.streamingContent;
      const toolsUsed = state.toolsUsed.length > 0 ? [...state.toolsUsed] : undefined;

      if (!content && !suggestedTask && !toolsUsed) {
        return { streamingContent: '', streamingThinking: '', toolsUsed: [] };
      }

      // Attach provider info from current session config
      const config = state.session?.modelConfig;
      const newMessage: InsightsChatMessage = {
        id: `msg-${Date.now()}`,
        role: 'assistant',
        content,
        timestamp: new Date(),
        suggestedTask,
        toolsUsed,
        provider: config?.provider,
        providerModel: config?.model,
      };

      if (!state.session) {
        return {
          streamingContent: '',
          streamingThinking: '',
          toolsUsed: [],
          session: {
            id: `session-${Date.now()}`,
            projectId: '',
            messages: [newMessage],
            createdAt: new Date(),
            updatedAt: new Date()
          }
        };
      }

      return {
        streamingContent: '',
        streamingThinking: '',
        toolsUsed: [],
        session: {
          ...state.session,
          messages: [...state.session.messages, newMessage],
          updatedAt: new Date()
        }
      };
    }),

  clearSession: () =>
    set({
      session: null,
      status: initialStatus,
      pendingMessage: '',
      streamingContent: '',
      streamingThinking: '',
      currentTool: null,
      toolsUsed: []
    })
}));

// Helper functions

export async function loadInsightsSessions(projectId: string): Promise<void> {
  const store = useInsightsStore.getState();
  store.setLoadingSessions(true);

  try {
    const result = await window.API.listInsightsSessions(projectId);
    if (result.success && result.data) {
      store.setSessions(result.data);
    } else {
      store.setSessions([]);
    }
  } finally {
    store.setLoadingSessions(false);
  }
}

export async function loadInsightsProviders(projectId: string): Promise<void> {
  const store = useInsightsStore.getState();
  store.setLoadingProviders(true);

  try {
    const result = await window.API.detectInsightsProviders(projectId);
    if (result.success && result.data) {
      store.setAvailableProviders(result.data);
    } else {
      store.setAvailableProviders([]);
    }
  } catch {
    store.setAvailableProviders([]);
  } finally {
    store.setLoadingProviders(false);
  }
}

export async function loadInsightsSession(projectId: string): Promise<void> {
  const result = await window.API.getInsightsSession(projectId);
  if (result.success && result.data) {
    useInsightsStore.getState().setSession(result.data);
  } else {
    useInsightsStore.getState().setSession(null);
  }
  // Also load the sessions list
  await loadInsightsSessions(projectId);
}

export function sendMessage(projectId: string, message: string, attachments?: ChatAttachment[], modelConfig?: InsightsModelConfig, branch?: string, repo?: string): void {
  const store = useInsightsStore.getState();
  const session = store.session;

  // Add user message to session
  const userMessage: InsightsChatMessage = {
    id: `msg-${Date.now()}`,
    role: 'user',
    content: message,
    timestamp: new Date(),
    attachments: attachments && attachments.length > 0 ? attachments : undefined
  };
  store.addMessage(userMessage);

  // Clear pending and set status
  store.setPendingMessage('');
  store.clearStreamingContent();
  store.clearStreamingThinking(); // Clear reasoning from previous response
  store.clearToolsUsed(); // Clear tools from previous response
  store.setLastMetrics(null); // Clear metrics from previous response
  store.setStatus({
    phase: 'thinking',
    message: 'Processing your message...'
  });

  // Use provided modelConfig, or fall back to session's config, or use defaults
  // Ensure provider and model are always set so the backend never gets undefined
  const configToUse = modelConfig || session?.modelConfig;
  const configWithProvider = {
    provider: 'claude' as const,
    model: DEFAULT_FEATURE_MODELS.insights,
    thinkingLevel: DEFAULT_FEATURE_THINKING.insights,
    codeSearch: 'auto' as const,
    ...configToUse,
  };

  // Send to main process (branch grounds the chat in a read-only worktree;
  // repo scopes a multi-repo project's grounding to a chosen child repo)
  window.API.sendInsightsMessage(projectId, message, attachments, configWithProvider as InsightsModelConfig, branch, repo);
}

export async function stopMessage(projectId: string): Promise<void> {
  const store = useInsightsStore.getState();
  try {
    await window.API.stopInsightsMessage(projectId);
  } catch {
    // Ignore errors — the task may have already finished
  }
  // Finalize any partial streaming content
  store.setCurrentTool(null);
  store.finalizeStreamingMessage();
  store.setStatus({ phase: 'idle', message: '' });
}

export async function clearSession(projectId: string): Promise<void> {
  const result = await window.API.clearInsightsSession(projectId);
  if (result.success) {
    useInsightsStore.getState().clearSession();
    // Reload sessions list and current session
    await loadInsightsSession(projectId);
  }
}

export async function newSession(projectId: string): Promise<void> {
  const result = await window.API.newInsightsSession(projectId);
  if (result.success && result.data) {
    useInsightsStore.getState().setSession(result.data);
    // Reload sessions list
    await loadInsightsSessions(projectId);
  }
}

export async function switchSession(projectId: string, sessionId: string): Promise<void> {
  const result = await window.API.switchInsightsSession(projectId, sessionId);
  if (result.success && result.data) {
    useInsightsStore.getState().setSession(result.data);
    // Reset streaming state when switching sessions
    useInsightsStore.getState().clearStreamingContent();
    useInsightsStore.getState().clearStreamingThinking();
    useInsightsStore.getState().clearToolsUsed();
    useInsightsStore.getState().setCurrentTool(null);
    useInsightsStore.getState().setStatus({ phase: 'idle', message: '' });
  }
}

export async function deleteSession(projectId: string, sessionId: string): Promise<boolean> {
  const result = await window.API.deleteInsightsSession(projectId, sessionId);
  if (result.success) {
    const data = result.data as { switchedTo?: string } | undefined;
    if (data?.switchedTo) {
      // Switch to the next available session
      await switchSession(projectId, data.switchedTo);
    } else {
      // No sessions remain — clear the view
      useInsightsStore.getState().clearSession();
    }
    // Reload sessions list
    await loadInsightsSessions(projectId);
    return true;
  }
  return false;
}

export async function renameSession(projectId: string, sessionId: string, newTitle: string): Promise<boolean> {
  const result = await window.API.renameInsightsSession(projectId, sessionId, newTitle);
  if (result.success) {
    // Reload sessions list to reflect the change
    await loadInsightsSessions(projectId);
    return true;
  }
  return false;
}

export async function updateModelConfig(projectId: string, sessionId: string, modelConfig: InsightsModelConfig): Promise<boolean> {
  const result = await window.API.updateInsightsModelConfig(projectId, sessionId, modelConfig);
  if (result.success) {
    // Update local session state
    const store = useInsightsStore.getState();
    if (store.session?.id === sessionId) {
      store.setSession({
        ...store.session,
        modelConfig,
        updatedAt: new Date()
      });
    }
    // Reload sessions list to reflect the change
    await loadInsightsSessions(projectId);
    return true;
  }
  return false;
}

export async function createTaskFromSuggestion(
  projectId: string,
  title: string,
  description: string,
  metadata?: TaskMetadata
): Promise<Task | null> {
  const result = await window.API.createTaskFromInsights(
    projectId,
    title,
    description,
    metadata
  );

  if (result.success && result.data) {
    return result.data;
  }
  return null;
}

export async function generateTaskFromChat(
  projectId: string,
  modelConfig?: InsightsModelConfig
): Promise<{ title: string; description: string } | null> {
  const result = await window.API.generateTaskFromChat(projectId, modelConfig);
  if (result.success && result.data) {
    return result.data;
  }
  return null;
}

// IPC listener setup - call this once when the app initializes
export function setupInsightsListeners(): () => void {
  const store = useInsightsStore.getState;

  // Listen for streaming chunks
  const unsubStreamChunk = window.API.onInsightsStreamChunk(
    (_projectId, chunk: InsightsStreamChunk) => {
      // Ignore events from the generate-task sentinel to avoid polluting the chat
      if (_projectId.startsWith('__gen_task_')) return;
      switch (chunk.type) {
        case 'text':
          if (chunk.content) {
            store().appendStreamingContent(chunk.content);
            store().setCurrentTool(null); // Clear tool when receiving text
            store().setStatus({
              phase: 'streaming',
              message: 'Receiving response...'
            });
          }
          break;
        case 'thinking':
          if (chunk.content) {
            store().appendStreamingThinking(chunk.content);
            // Keep status as 'thinking' — the dedicated reasoning panel renders
            // these tokens; we don't flip to 'streaming' until real text arrives.
          }
          break;
        case 'tool_start':
          if (chunk.tool) {
            store().setCurrentTool({
              name: chunk.tool.name,
              input: chunk.tool.input
            });
            // Record this tool usage for history
            store().addToolUsage({
              name: chunk.tool.name,
              input: chunk.tool.input
            });
            store().setStatus({
              phase: 'streaming',
              message: `Using ${chunk.tool.name}...`
            });
          }
          break;
        case 'tool_input':
          // Full tool arguments arrived (e.g. the SQL query) once the tool's
          // input block closed — backfill them onto the live indicator + history.
          if (chunk.tool) {
            store().setCurrentTool({
              name: chunk.tool.name,
              input: chunk.tool.input
            });
            store().updateLastTool({ input: chunk.tool.input });
          }
          break;
        case 'tool_end':
          // Attach the tool's output to the last recorded tool, then clear the
          // live indicator.
          if (chunk.result !== undefined || chunk.isError !== undefined) {
            store().updateLastTool({
              result: chunk.result,
              isError: chunk.isError
            });
          }
          store().setCurrentTool(null);
          break;
        case 'task_suggestion':
          // Finalize the message with task suggestion
          store().setCurrentTool(null);
          store().finalizeStreamingMessage(chunk.suggestedTask);
          break;
        case 'done':
          // Capture metrics from the done event (if available)
          if (chunk.metrics) {
            store().setLastMetrics(chunk.metrics);
          }
          // Finalize any remaining content
          store().setCurrentTool(null);
          store().finalizeStreamingMessage();
          store().setStatus({
            phase: 'complete',
            message: ''
          });
          break;
        case 'error':
          store().setCurrentTool(null);
          store().setStatus({
            phase: 'error',
            error: chunk.error
          });
          break;
      }
    }
  );

  // Listen for status updates
  const unsubStatus = window.API.onInsightsStatus((_projectId, status) => {
    store().setStatus(status);
  });

  // Listen for errors
  const unsubError = window.API.onInsightsError((_projectId, error) => {
    store().setStatus({
      phase: 'error',
      error
    });
  });

  // Return cleanup function
  return () => {
    unsubStreamChunk();
    unsubStatus();
    unsubError();
  };
}
