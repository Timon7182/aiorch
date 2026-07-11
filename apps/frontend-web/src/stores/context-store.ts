import { create } from 'zustand';
import type {
  ProjectIndex,
  GraphitiMemoryStatus,
  GraphitiMemoryState,
  MemoryEpisode,
  ContextSearchResult,
  GraphMemoryEpisode,
  GraphMemoryKind
} from '../shared/types';

interface ContextState {
  // Project Index
  projectIndex: ProjectIndex | null;
  indexLoading: boolean;
  indexError: string | null;

  // Memory Status
  memoryStatus: GraphitiMemoryStatus | null;
  memoryState: GraphitiMemoryState | null;
  memoryLoading: boolean;
  memoryError: string | null;

  // Recent Memories
  recentMemories: MemoryEpisode[];
  memoriesLoading: boolean;

  // Search
  searchResults: ContextSearchResult[];
  searchLoading: boolean;
  searchQuery: string;

  // Graph memory (Graphiti knowledge graph — visible/editable)
  graphEpisodes: GraphMemoryEpisode[];
  graphLoading: boolean;
  graphAvailable: boolean;
  graphReason: string | null;
  graphGroupId: string | null;
  graphSearchResults: ContextSearchResult[];
  graphSearchLoading: boolean;
  graphSearchQuery: string;

  // Actions
  setProjectIndex: (index: ProjectIndex | null) => void;
  setIndexLoading: (loading: boolean) => void;
  setIndexError: (error: string | null) => void;
  setMemoryStatus: (status: GraphitiMemoryStatus | null) => void;
  setMemoryState: (state: GraphitiMemoryState | null) => void;
  setMemoryLoading: (loading: boolean) => void;
  setMemoryError: (error: string | null) => void;
  setRecentMemories: (memories: MemoryEpisode[]) => void;
  setMemoriesLoading: (loading: boolean) => void;
  setSearchResults: (results: ContextSearchResult[]) => void;
  setSearchLoading: (loading: boolean) => void;
  setSearchQuery: (query: string) => void;
  setGraphEpisodes: (episodes: GraphMemoryEpisode[]) => void;
  setGraphLoading: (loading: boolean) => void;
  setGraphMeta: (meta: { available: boolean; reason: string | null; groupId: string | null }) => void;
  setGraphSearchResults: (results: ContextSearchResult[]) => void;
  setGraphSearchLoading: (loading: boolean) => void;
  setGraphSearchQuery: (query: string) => void;
  clearAll: () => void;
}

export const useContextStore = create<ContextState>((set) => ({
  // Project Index
  projectIndex: null,
  indexLoading: false,
  indexError: null,

  // Memory Status
  memoryStatus: null,
  memoryState: null,
  memoryLoading: false,
  memoryError: null,

  // Recent Memories
  recentMemories: [],
  memoriesLoading: false,

  // Search
  searchResults: [],
  searchLoading: false,
  searchQuery: '',

  // Graph memory
  graphEpisodes: [],
  graphLoading: false,
  graphAvailable: false,
  graphReason: null,
  graphGroupId: null,
  graphSearchResults: [],
  graphSearchLoading: false,
  graphSearchQuery: '',

  // Actions
  setProjectIndex: (index) => set({ projectIndex: index }),
  setIndexLoading: (loading) => set({ indexLoading: loading }),
  setIndexError: (error) => set({ indexError: error }),
  setMemoryStatus: (status) => set({ memoryStatus: status }),
  setMemoryState: (state) => set({ memoryState: state }),
  setMemoryLoading: (loading) => set({ memoryLoading: loading }),
  setMemoryError: (error) => set({ memoryError: error }),
  setRecentMemories: (memories) => set({ recentMemories: memories }),
  setMemoriesLoading: (loading) => set({ memoriesLoading: loading }),
  setSearchResults: (results) => set({ searchResults: results }),
  setSearchLoading: (loading) => set({ searchLoading: loading }),
  setSearchQuery: (query) => set({ searchQuery: query }),
  setGraphEpisodes: (episodes) => set({ graphEpisodes: episodes }),
  setGraphLoading: (loading) => set({ graphLoading: loading }),
  setGraphMeta: (meta) =>
    set({ graphAvailable: meta.available, graphReason: meta.reason, graphGroupId: meta.groupId }),
  setGraphSearchResults: (results) => set({ graphSearchResults: results }),
  setGraphSearchLoading: (loading) => set({ graphSearchLoading: loading }),
  setGraphSearchQuery: (query) => set({ graphSearchQuery: query }),
  clearAll: () =>
    set({
      projectIndex: null,
      indexLoading: false,
      indexError: null,
      memoryStatus: null,
      memoryState: null,
      memoryLoading: false,
      memoryError: null,
      recentMemories: [],
      memoriesLoading: false,
      searchResults: [],
      searchLoading: false,
      searchQuery: '',
      graphEpisodes: [],
      graphLoading: false,
      graphAvailable: false,
      graphReason: null,
      graphGroupId: null,
      graphSearchResults: [],
      graphSearchLoading: false,
      graphSearchQuery: ''
    })
}));

/**
 * Load project context (project index + memory status)
 */
export async function loadProjectContext(projectId: string): Promise<void> {
  const store = useContextStore.getState();
  store.setIndexLoading(true);
  store.setMemoryLoading(true);
  store.setIndexError(null);
  store.setMemoryError(null);

  try {
    const result = await window.API.getProjectContext(projectId);
    if (result.success && result.data) {
      store.setProjectIndex(result.data.projectIndex);
      store.setMemoryStatus(result.data.memoryStatus);
      store.setMemoryState(result.data.memoryState);
      store.setRecentMemories(result.data.recentMemories || []);
    } else {
      store.setIndexError(result.error || 'Failed to load project context');
    }
  } catch (error) {
    store.setIndexError(error instanceof Error ? error.message : 'Unknown error');
  } finally {
    store.setIndexLoading(false);
    store.setMemoryLoading(false);
  }
}

/**
 * Refresh project index by re-running analyzer
 */
export async function refreshProjectIndex(projectId: string): Promise<void> {
  const store = useContextStore.getState();
  store.setIndexLoading(true);
  store.setIndexError(null);

  try {
    const result = await window.API.refreshProjectIndex(projectId);
    if (result.success && result.data) {
      store.setProjectIndex(result.data);
    } else {
      store.setIndexError(result.error || 'Failed to refresh project index');
    }
  } catch (error) {
    store.setIndexError(error instanceof Error ? error.message : 'Unknown error');
  } finally {
    store.setIndexLoading(false);
  }
}

/**
 * Search memories using semantic search
 */
export async function searchMemories(
  projectId: string,
  query: string
): Promise<void> {
  const store = useContextStore.getState();
  store.setSearchQuery(query);

  if (!query.trim()) {
    store.setSearchResults([]);
    return;
  }

  store.setSearchLoading(true);

  try {
    const result = await window.API.searchMemories(projectId, query);
    if (result.success && result.data) {
      store.setSearchResults(result.data);
    } else {
      store.setSearchResults([]);
    }
  } catch (_error) {
    store.setSearchResults([]);
  } finally {
    store.setSearchLoading(false);
  }
}

/**
 * Load recent memories
 */
export async function loadRecentMemories(
  projectId: string,
  limit: number = 20
): Promise<void> {
  const store = useContextStore.getState();
  store.setMemoriesLoading(true);

  try {
    const result = await window.API.getRecentMemories(projectId, limit);
    if (result.success && result.data) {
      store.setRecentMemories(result.data);
    }
  } catch (_error) {
    // Silently fail - memories are optional
  } finally {
    store.setMemoriesLoading(false);
  }
}

/**
 * Load raw graph-memory episodes (Graphiti knowledge graph)
 */
export async function loadGraphMemory(
  projectId: string,
  limit: number = 50
): Promise<void> {
  const store = useContextStore.getState();
  store.setGraphLoading(true);

  try {
    const result = await window.API.getMemoryEpisodes(projectId, limit);
    if (result.success && result.data) {
      store.setGraphEpisodes(result.data.episodes || []);
      store.setGraphMeta({
        available: !!result.data.available,
        reason: result.data.reason || null,
        groupId: result.data.groupId || null
      });
    } else {
      store.setGraphEpisodes([]);
      store.setGraphMeta({ available: false, reason: result.error || null, groupId: null });
    }
  } catch (error) {
    store.setGraphEpisodes([]);
    store.setGraphMeta({
      available: false,
      reason: error instanceof Error ? error.message : 'Unknown error',
      groupId: null
    });
  } finally {
    store.setGraphLoading(false);
  }
}

/**
 * Semantic search over the graph memory
 */
export async function searchGraphMemory(
  projectId: string,
  query: string
): Promise<void> {
  const store = useContextStore.getState();
  store.setGraphSearchQuery(query);

  if (!query.trim()) {
    store.setGraphSearchResults([]);
    return;
  }

  store.setGraphSearchLoading(true);

  try {
    const result = await window.API.searchMemoryGraph(projectId, query);
    if (result.success && result.data && result.data.available) {
      store.setGraphSearchResults(result.data.results || []);
    } else {
      store.setGraphSearchResults([]);
    }
  } catch (_error) {
    store.setGraphSearchResults([]);
  } finally {
    store.setGraphSearchLoading(false);
  }
}

/**
 * Add a fact/pattern/gotcha to the graph memory, then reload the list.
 * Returns an error string on failure, or null on success.
 */
export async function addGraphMemory(
  projectId: string,
  content: string,
  kind: GraphMemoryKind = 'fact'
): Promise<string | null> {
  try {
    const result = await window.API.addMemoryEpisode(projectId, content, kind);
    if (result.success) {
      await loadGraphMemory(projectId);
      return null;
    }
    return result.error || 'Failed to save memory';
  } catch (error) {
    return error instanceof Error ? error.message : 'Unknown error';
  }
}

/**
 * Delete a graph-memory episode by uuid, then reload the list.
 * Returns an error string on failure, or null on success.
 */
export async function deleteGraphMemory(
  projectId: string,
  uuid: string
): Promise<string | null> {
  try {
    const result = await window.API.deleteMemoryEpisode(projectId, uuid);
    if (result.success) {
      await loadGraphMemory(projectId);
      return null;
    }
    return result.error || 'Failed to delete memory';
  } catch (error) {
    return error instanceof Error ? error.message : 'Unknown error';
  }
}
