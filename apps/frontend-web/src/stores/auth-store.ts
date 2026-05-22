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

import { clearAuthToken, getAuthToken, setAuthToken } from '../lib/auth';

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  user: { id: string; email: string; name: string; role: string } | null;

  login: (token: string) => Promise<boolean>;
  loginWithCredentials: (email: string, password: string) => Promise<boolean>;
  register: (email: string, name: string, password: string) => Promise<boolean>;
  logout: () => void;
  checkAuth: () => Promise<boolean>;
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
      user: { id: string; email: string; name: string; role: string };
      access_token: string;
      refresh_token?: string;
    };
    setAuthToken(data.access_token);
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
        set({ isAuthenticated: false, error: null, user: null });
      },

      checkAuth: async () => {
        const token = getAuthToken();
        if (!token) {
          set({ isAuthenticated: false, isLoading: false });
          return false;
        }
        set({ isLoading: true });
        try {
          const response = await fetch('/api/settings', {
            headers: { Authorization: `Bearer ${token}` },
          });
          if (response.ok) {
            set({ isAuthenticated: true, isLoading: false });
            return true;
          }
          if (response.status === 401 || response.status === 403) {
            clearAuthToken();
            set({ isAuthenticated: false, isLoading: false, user: null });
          } else {
            set({ isAuthenticated: false, isLoading: false });
          }
          return false;
        } catch {
          set({ isAuthenticated: false, isLoading: false });
          return false;
        }
      },
    }),
    {
      name: 'magestic-ai-auth',
      partialize: () => ({}),
    },
  ),
);
