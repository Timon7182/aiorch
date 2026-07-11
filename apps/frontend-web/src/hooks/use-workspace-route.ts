import { matchPath, useLocation } from 'react-router-dom';
import type { SidebarView } from '../components/Sidebar';

/**
 * Views that are NOT scoped to a project. These live at the top-level path
 * (e.g. `/hermes`) instead of under `/p/:projectId/...`.
 */
export const GLOBAL_VIEWS = ['members', 'transcripts', 'admin'] as const;

/**
 * Views that require a selected project. They live under
 * `/p/:projectId/:view`.
 */
export const PROJECT_VIEWS = [
  'overview',
  'kanban',
  'terminals',
  'editor',
  'context',
  'github-issues',
  'github-prs',
  'insights',
  'worktrees',
  'agent-tools',
  'skills',
  'docs',
  'usage',
] as const;

export const DEFAULT_PROJECT_VIEW: SidebarView = 'kanban';

export function isGlobalView(view: string | null | undefined): view is SidebarView {
  return !!view && (GLOBAL_VIEWS as readonly string[]).includes(view);
}

export interface WorkspaceRoute {
  /** Project id from the URL, or null for global views / root. */
  projectId: string | null;
  /** The active sidebar view derived from the URL, or null. */
  view: SidebarView | null;
  /** Task id when on a task detail page (`/p/:projectId/tasks/:taskId`). */
  taskId: string | null;
  /** True when the URL is a project-independent global view. */
  isGlobalView: boolean;
}

/**
 * Parse the current location into the workspace's logical coordinates
 * (project / view / task). This is the single source of truth for which
 * project and view are active — replacing the old in-memory `activeView`
 * and `selectedTaskId` state so every screen is linkable.
 *
 * We parse the path manually (instead of nesting `<Routes>`) so heavy,
 * stateful views like the terminal grid can stay mounted across navigation.
 */
export function useWorkspaceRoute(): WorkspaceRoute {
  const { pathname } = useLocation();

  const taskMatch = matchPath('/p/:projectId/tasks/:taskId', pathname);
  if (taskMatch) {
    return {
      projectId: taskMatch.params.projectId ?? null,
      view: null,
      taskId: taskMatch.params.taskId ?? null,
      isGlobalView: false,
    };
  }

  const viewMatch = matchPath('/p/:projectId/:view', pathname);
  if (viewMatch) {
    return {
      projectId: viewMatch.params.projectId ?? null,
      view: (viewMatch.params.view as SidebarView) ?? null,
      taskId: null,
      isGlobalView: false,
    };
  }

  const projectMatch = matchPath('/p/:projectId', pathname);
  if (projectMatch) {
    return {
      projectId: projectMatch.params.projectId ?? null,
      view: null,
      taskId: null,
      isGlobalView: false,
    };
  }

  const globalMatch = matchPath('/:view', pathname);
  if (globalMatch && isGlobalView(globalMatch.params.view)) {
    return {
      projectId: null,
      view: globalMatch.params.view as SidebarView,
      taskId: null,
      isGlobalView: true,
    };
  }

  return { projectId: null, view: null, taskId: null, isGlobalView: false };
}
