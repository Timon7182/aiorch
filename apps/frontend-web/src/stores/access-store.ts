/**
 * Access store — the current user's project/page grants.
 *
 * Backed by `GET /api/auth/my-access`. Admins and ungranted users are
 * "unrestricted" (the UI shows everything). Otherwise `projects` maps each
 * granted project id to the list of allowed page (sidebar view) ids, or `null`
 * meaning all pages of that project.
 *
 * This is frontend filtering only — it hides projects/pages the user has not
 * been granted; the backend does not yet hard-enforce per-project routes.
 */

import { create } from 'zustand';

import { get } from '../lib/api-client';

interface MyAccess {
  is_admin: boolean;
  unrestricted: boolean;
  projects: Record<string, string[] | null>;
}

interface AccessState {
  loaded: boolean;
  isAdmin: boolean;
  unrestricted: boolean;
  /** projectId -> allowed page ids, or null for all pages. */
  projects: Record<string, string[] | null>;

  loadAccess: () => Promise<void>;
  /** Whether the user may see/open a given project. */
  canSeeProject: (projectId: string) => boolean;
  /** Whether the user may open a given page within a project. */
  canSeePage: (projectId: string | null | undefined, page: string) => boolean;
}

export const useAccessStore = create<AccessState>((set, getState) => ({
  loaded: false,
  isAdmin: false,
  unrestricted: true,
  projects: {},

  loadAccess: async () => {
    const result = await get<MyAccess>('/auth/my-access');
    if (result.success && result.data) {
      set({
        loaded: true,
        isAdmin: result.data.is_admin,
        unrestricted: result.data.unrestricted,
        projects: result.data.projects ?? {},
      });
    } else {
      // On failure, fail open (don't lock the user out of their own UI).
      set({ loaded: true, isAdmin: false, unrestricted: true, projects: {} });
    }
  },

  canSeeProject: (projectId) => {
    const s = getState();
    if (s.unrestricted) return true;
    return Object.prototype.hasOwnProperty.call(s.projects, projectId);
  },

  canSeePage: (projectId, page) => {
    const s = getState();
    if (s.unrestricted) return true;
    if (!projectId) return true;
    if (!Object.prototype.hasOwnProperty.call(s.projects, projectId)) return false;
    const pages = s.projects[projectId];
    if (pages === null || pages === undefined) return true; // all pages
    return pages.includes(page);
  },
}));

/** Load access grants; safe to call repeatedly. */
export async function loadAccess(): Promise<void> {
  await useAccessStore.getState().loadAccess();
}
