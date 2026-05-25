/**
 * Token usage store.
 *
 * Fetches per-task and per-project usage from `/api/usage/*`, then merges live
 * `task:usage` WebSocket events from the agent backend so dashboards update in
 * real time without re-fetching.
 */

import { create } from 'zustand';

// Aliased: Zustand's store factory passes its own `get` (state getter) into
// the slice, which would shadow this import. Use `apiGet` for HTTP calls.
import { get as apiGet } from '../lib/api-client';
import { createLogger } from '../lib/logger';
import {
  emptyTotals,
  type ProjectUsage,
  type TaskUsage,
  type UsageEvent,
  type UsageTotals,
} from '../shared/types/usage';

const log = createLogger('usage-store');

interface UsageState {
  // Per-project usage rollup, keyed by projectId.
  projectUsage: Record<string, ProjectUsage>;
  // Per-task usage, keyed by full taskId (projectId:specId).
  taskUsage: Record<string, TaskUsage>;
  // Loading flags so the UI can show skeletons without flicker.
  loadingProject: Record<string, boolean>;
  loadingTask: Record<string, boolean>;
  error: string | null;

  fetchProjectUsage: (projectId: string) => Promise<void>;
  fetchTaskUsage: (taskId: string) => Promise<void>;
  ingestUsageEvent: (taskId: string, event: UsageEvent) => void;
  /** Live merge for non-task features (Hermes, Insights chat). */
  ingestProjectUsageEvent: (projectId: string, event: UsageEvent) => void;
  clearProject: (projectId: string) => void;
}

function addTotals(a: UsageTotals, b: Partial<UsageTotals>): UsageTotals {
  return {
    input_tokens: a.input_tokens + (b.input_tokens ?? 0),
    output_tokens: a.output_tokens + (b.output_tokens ?? 0),
    cache_read_input_tokens:
      a.cache_read_input_tokens + (b.cache_read_input_tokens ?? 0),
    cache_creation_input_tokens:
      a.cache_creation_input_tokens + (b.cache_creation_input_tokens ?? 0),
    cost_usd: a.cost_usd + (b.cost_usd ?? 0),
    calls: a.calls + (b.calls ?? 1),
  };
}

function bumpBucket(
  buckets: Record<string, UsageTotals>,
  key: string,
  event: UsageEvent,
): Record<string, UsageTotals> {
  const existing = buckets[key] ?? emptyTotals();
  return { ...buckets, [key]: addTotals(existing, { ...event, calls: 1 }) };
}

export const useUsageStore = create<UsageState>((set, get) => ({
  projectUsage: {},
  taskUsage: {},
  loadingProject: {},
  loadingTask: {},
  error: null,

  fetchProjectUsage: async (projectId: string) => {
    if (!projectId) return;
    set((s) => ({
      loadingProject: { ...s.loadingProject, [projectId]: true },
    }));
    try {
      const res = await apiGet<ProjectUsage>(`/usage/projects/${projectId}`);
      if (res.success && res.data) {
        set((s) => ({
          projectUsage: { ...s.projectUsage, [projectId]: res.data! },
        }));
      } else if (!res.success) {
        log.warn(`fetchProjectUsage failed: ${res.error ?? 'unknown'}`);
        set({ error: res.error ?? 'Failed to load project usage' });
      }
    } finally {
      set((s) => ({
        loadingProject: { ...s.loadingProject, [projectId]: false },
      }));
    }
  },

  fetchTaskUsage: async (taskId: string) => {
    if (!taskId) return;
    set((s) => ({ loadingTask: { ...s.loadingTask, [taskId]: true } }));
    try {
      const res = await apiGet<TaskUsage>(
        `/usage/tasks/${encodeURIComponent(taskId)}`,
      );
      if (res.success && res.data) {
        set((s) => ({
          taskUsage: { ...s.taskUsage, [taskId]: res.data! },
        }));
      } else if (!res.success) {
        log.warn(`fetchTaskUsage failed: ${res.error ?? 'unknown'}`);
      }
    } finally {
      set((s) => ({ loadingTask: { ...s.loadingTask, [taskId]: false } }));
    }
  },

  /**
   * Apply a live WebSocket usage event to in-memory state so per-task pills
   * and the project dashboard card update without an HTTP round-trip.
   */
  ingestUsageEvent: (taskId, event) => {
    const [projectId, specId] = taskId.includes(':')
      ? taskId.split(':', 2)
      : [taskId, taskId];

    set((state) => {
      // --- Task-level update -------------------------------------------------
      const existingTask = state.taskUsage[taskId];
      const taskTotals = addTotals(
        existingTask?.totals ?? emptyTotals(),
        { ...event, calls: 1 },
      );
      const taskByPhase = bumpBucket(
        existingTask?.byPhase ?? {},
        event.phase ?? 'unknown',
        event,
      );
      const taskByModel = bumpBucket(
        existingTask?.byModel ?? {},
        event.model ?? 'unknown',
        event,
      );
      // Keep the last 200 events on the client to power per-task charts;
      // bounded so memory stays predictable over a long session.
      const taskEvents = [...(existingTask?.events ?? []), event].slice(-200);

      const nextTask: TaskUsage = {
        taskId,
        projectId,
        specId,
        hasData: true,
        totals: taskTotals,
        byPhase: taskByPhase,
        byModel: taskByModel,
        events: taskEvents,
        createdAt: existingTask?.createdAt,
        updatedAt: event.timestamp,
      };

      // --- Project-level update ---------------------------------------------
      const existingProject = state.projectUsage[projectId];
      let nextProject = existingProject;
      if (existingProject) {
        const projTotals = addTotals(existingProject.totals, {
          ...event,
          calls: 1,
        });
        const projByPhase = bumpBucket(
          existingProject.byPhase,
          event.phase ?? 'unknown',
          event,
        );
        const projByModel = bumpBucket(
          existingProject.byModel,
          event.model ?? 'unknown',
          event,
        );

        // Update the matching per-task summary, or append a new one if this
        // is the first usage event we've seen for that task.
        const existingSummaries = existingProject.tasks ?? [];
        const summaryIndex = existingSummaries.findIndex(
          (t) => t.taskId === taskId,
        );
        const nextSummary = {
          taskId,
          specId,
          totals: taskTotals,
          hasData: true,
          updatedAt: event.timestamp,
        };
        const nextSummaries =
          summaryIndex >= 0
            ? existingSummaries.map((t, i) =>
                i === summaryIndex ? nextSummary : t,
              )
            : [...existingSummaries, nextSummary];

        const projByFeature = bumpBucket(
          existingProject.byFeature ?? {},
          event.feature ?? 'agent',
          event,
        );

        nextProject = {
          ...existingProject,
          totals: projTotals,
          byPhase: projByPhase,
          byModel: projByModel,
          byFeature: projByFeature,
          tasks: nextSummaries,
          tasksWithData: nextSummaries.filter((t) => t.hasData).length,
          taskCount: Math.max(existingProject.taskCount, nextSummaries.length),
        };
      }

      return {
        taskUsage: { ...state.taskUsage, [taskId]: nextTask },
        projectUsage: nextProject
          ? { ...state.projectUsage, [projectId]: nextProject }
          : state.projectUsage,
      };
    });
  },

  ingestProjectUsageEvent: (projectId, event) => {
    // Project-scoped usage (Hermes, Insights chat) — no taskId, so we only
    // bump the project rollup, not any per-task entry. Skipped silently when
    // the project hasn't been fetched yet (next fetch will pick it up).
    set((state) => {
      const existing = state.projectUsage[projectId];
      if (!existing) return state;
      const featureKey = event.feature ?? 'session';
      return {
        projectUsage: {
          ...state.projectUsage,
          [projectId]: {
            ...existing,
            totals: addTotals(existing.totals, { ...event, calls: 1 }),
            byPhase: bumpBucket(
              existing.byPhase,
              event.phase ?? featureKey,
              event,
            ),
            byModel: bumpBucket(
              existing.byModel,
              event.model ?? 'unknown',
              event,
            ),
            byFeature: bumpBucket(
              existing.byFeature ?? {},
              featureKey,
              event,
            ),
          },
        },
      };
    });
  },

  clearProject: (projectId: string) => {
    set((state) => {
      const { [projectId]: _removed, ...rest } = state.projectUsage;
      return { projectUsage: rest };
    });
  },
}));

/**
 * Subscribe the store to live task:usage WebSocket events.
 *
 * Returns an unsubscribe function. Call once at app bootstrap (after the
 * web API adapter is initialized).
 */
export function initUsageWebSocketBridge(): () => void {
  const api = (window as Window & { API?: typeof window.API }).API;
  // Subscribe to project:usage too — fire-and-forget, ignores absence.
  const unsubProject = api?.onProjectUsage?.((projectId, usage) => {
    useUsageStore.getState().ingestProjectUsageEvent(projectId, usage);
  });
  if (!api?.onTaskUsage) {
    log.warn('window.API.onTaskUsage not available — usage live updates disabled');
    return () => unsubProject?.();
  }
  const unsubTask = api.onTaskUsage((taskId, usage) => {
    useUsageStore.getState().ingestUsageEvent(taskId, usage);
  });
  return () => {
    unsubTask();
    unsubProject?.();
  };
}

/** Convenience selectors. */
export const selectProjectUsage = (projectId: string) =>
  (state: UsageState) => state.projectUsage[projectId];

export const selectTaskUsage = (taskId: string) =>
  (state: UsageState) => state.taskUsage[taskId];
