/**
 * Shown to a freshly-registered account whose status is still "pending".
 *
 * The backend gates all project data behind admin approval, so instead of
 * dropping the user into an app that 403s everywhere we show this waiting
 * screen. It polls /auth/me; the moment an admin approves the account
 * (status → "active") the auth store updates and App.tsx swaps in the real
 * workspace automatically.
 */

import { useEffect, useState } from 'react';
import { Clock, LogOut, RefreshCw } from 'lucide-react';

import { useAuthStore } from '../stores/auth-store';

const POLL_INTERVAL_MS = 15_000;

export function PendingApprovalScreen() {
  const user = useAuthStore((s) => s.user);
  const refreshUser = useAuthStore((s) => s.refreshUser);
  const logout = useAuthStore((s) => s.logout);
  const [checking, setChecking] = useState(false);

  // Poll for approval so the user doesn't have to refresh the page.
  useEffect(() => {
    const id = setInterval(() => {
      void refreshUser();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refreshUser]);

  async function checkNow() {
    setChecking(true);
    try {
      await refreshUser();
    } finally {
      setChecking(false);
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 text-center shadow-sm">
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-primary/15 text-primary">
          <Clock className="h-7 w-7" />
        </div>
        <h1 className="text-xl font-semibold">Waiting for approval</h1>
        <p className="mt-3 text-sm text-muted-foreground">
          Your account{user?.email ? ` (${user.email})` : ''} has been created and
          is awaiting approval from an administrator. You'll get access as soon
          as someone lets you in.
        </p>
        <p className="mt-2 text-xs text-muted-foreground">
          This page checks automatically — you can leave it open.
        </p>

        <div className="mt-6 flex items-center justify-center gap-3">
          <button
            type="button"
            onClick={() => void checkNow()}
            disabled={checking}
            className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${checking ? 'animate-spin' : ''}`} />
            {checking ? 'Checking…' : 'Check again'}
          </button>
          <button
            type="button"
            onClick={() => logout()}
            className="inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-accent"
          >
            <LogOut className="h-4 w-4" />
            Log out
          </button>
        </div>
      </div>
    </div>
  );
}
