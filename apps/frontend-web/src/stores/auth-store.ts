/**
 * Authentication store for web UI.
 *
 * Supports two auth paths:
 *   1. Email + password (preferred) — calls POST /api/auth/{login,register}
 *      and stores the returned access_token.
 *   2. Direct API token (legacy / service accounts) — validates against
 *      /api/health.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

import {
  clearAuthToken,
  clearRefreshToken,
  getAuthToken,
  setAuthToken,
  setRefreshToken,
} from '../lib/auth';

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  role: string;
  // "pending" until an admin approves the account, then "active". The app
  // shows the waiting screen instead of the workspace while pending.
  status?: string;
}

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  user: AuthUser | null;

  login: (token: string) => Promise<boolean>;
  loginWithCredentials: (email: string, password: string) => Promise<boolean>;
  register: (email: string, name: string, password: string) => Promise<boolean>;
  logout: () => void;
  checkAuth: () => Promise<boolean>;
  // Re-fetch the current user's profile (used by the pending screen to detect
  // approval and by checkAuth to repopulate `user` after a page reload).
  refreshUser: () => Promise<void>;
}

async function _tokenLogin(
  token: string,
  set: (s: Partial<AuthState>) => void,
): Promise<boolean> {
  set({ isLoading: true, error: null });
  try {
    const response = await fetch('/api/health', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (response.ok) {
      setAuthToken(token);
      set({ isAuthenticated: true, isLoading: false, user: null });
      return true;
    }
    set({ error: 'Invalid token', isLoading: false });
    return false;
  } catch (error) {
    set({
      error: error instanceof Error ? error.message : 'Login failed',
      isLoading: false,
    });
    return false;
  }
}

async function _credentialAuth(
  endpoint: '/api/auth/login' | '/api/auth/register',
  body: Record<string, string>,
  set: (s: Partial<AuthState>) => void,
): Promise<boolean> {
  set({ isLoading: true, error: null });
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let detail = `${endpoint} ${response.status}`;
      try {
        const data = (await response.json()) as { detail?: string | object };
        if (data?.detail) {
          detail =
            typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        }
      } catch {
        // body wasn't JSON; keep the status-line detail
      }
      set({ error: detail, isLoading: false });
      return false;
    }
    const data = (await response.json()) as {
      user: AuthUser;
      access_token: string;
      refresh_token?: string;
    };
    setAuthToken(data.access_token);
    if (data.refresh_token) {
      setRefreshToken(data.refresh_token);
    }
    set({
      isAuthenticated: true,
      isLoading: false,
      user: data.user,
      error: null,
    });
    return true;
  } catch (error) {
    set({
      error: error instanceof Error ? error.message : `${endpoint} failed`,
      isLoading: false,
    });
    return false;
  }
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      isAuthenticated: false,
      isLoading: false,
      error: null,
      user: null,

      login: (token) => _tokenLogin(token, set),

      loginWithCredentials: (email, password) =>
        _credentialAuth('/api/auth/login', { email, password }, set),

      register: (email, name, password) =>
        _credentialAuth('/api/auth/register', { email, name, password }, set),

      logout: () => {
        clearAuthToken();
        clearRefreshToken();
        set({ isAuthenticated: false, error: null, user: null });
      },

      checkAuth: async () => {
        // api-client.apiRequest already handles 401 → refresh → retry, so
        // route this probe through it. That way an expired-but-refreshable
        // session boots straight back in instead of bouncing to login.
        // Probe /auth/me (not /settings) so we also repopulate `user` —
        // including `status` — which the persist layer drops on reload. The
        // pending gate in App.tsx depends on this being set after a refresh.
        const { get } = await import('../lib/api-client');
        const token = getAuthToken();
        if (!token) {
          set({ isAuthenticated: false, isLoading: false });
          return false;
        }
        set({ isLoading: true });
        const result = await get('/auth/me');
        if (result.success) {
          set({
            isAuthenticated: true,
            isLoading: false,
            user: result.data as AuthUser,
          });
          return true;
        }
        // api-client cleared tokens on a failed refresh; mirror that here.
        set({ isAuthenticated: false, isLoading: false, user: null });
        return false;
      },

      refreshUser: async () => {
        const { get } = await import('../lib/api-client');
        const result = await get('/auth/me');
        if (result.success) {
          set({ user: result.data as AuthUser });
        }
      },
    }),
    {
      name: 'magestic-ai-auth',
      partialize: () => ({}),
    },
  ),
);
