import { useEffect } from 'react';
import {
  loadProjectContext,
  refreshProjectIndex,
  searchMemories,
  loadGraphMemory,
  searchGraphMemory,
  addGraphMemory,
  deleteGraphMemory
} from '../../stores/context-store';
import type { GraphMemoryKind } from '../../shared/types';

export function useProjectContext(projectId: string) {
  useEffect(() => {
    if (projectId) {
      loadProjectContext(projectId);
    }
  }, [projectId]);
}

export function useRefreshIndex(projectId: string) {
  return async () => {
    await refreshProjectIndex(projectId);
  };
}

export function useMemorySearch(projectId: string) {
  return async (query: string) => {
    if (query.trim()) {
      await searchMemories(projectId, query);
    }
  };
}

/**
 * Load the graph memory (Graphiti) episodes and expose CRUD + search handlers.
 */
export function useGraphMemory(projectId: string) {
  useEffect(() => {
    if (projectId) {
      loadGraphMemory(projectId);
    }
  }, [projectId]);

  return {
    reload: () => loadGraphMemory(projectId),
    search: (query: string) => searchGraphMemory(projectId, query),
    add: (content: string, kind: GraphMemoryKind) => addGraphMemory(projectId, content, kind),
    remove: (uuid: string) => deleteGraphMemory(projectId, uuid)
  };
}
