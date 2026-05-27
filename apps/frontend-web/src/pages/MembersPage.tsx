/**
 * Members page — manage organization access.
 * Lists user's orgs, shows members of the selected org, allows invite/role change/remove.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Trash2, UserPlus, RefreshCw, Building2, UserCheck, Check, X } from 'lucide-react';

import { getAuthHeaders } from '../lib/auth';

type Org = {
  id: string;
  name: string;
  slug: string;
  plan: string;
  member_count: number;
  user_role: 'viewer' | 'member' | 'admin' | 'owner';
};

type Member = {
  id: string;
  user_id: string;
  email: string;
  name: string;
  avatar_url: string | null;
  role: 'viewer' | 'member' | 'admin' | 'owner';
  joined_at: string;
};

type PendingUser = {
  id: string;
  email: string;
  name: string;
  created_at: string;
};

const ROLES: Member['role'][] = ['viewer', 'member', 'admin', 'owner'];

function roleLevel(r: Member['role']): number {
  return ROLES.indexOf(r);
}

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      ...getAuthHeaders(),
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = (await res.json()) as { detail?: string | object };
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function MembersPage() {
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [selectedOrgId, setSelectedOrgId] = useState<string | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // create-org form
  const [showCreate, setShowCreate] = useState(false);
  const [orgName, setOrgName] = useState('');

  // invite form
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<Member['role']>('member');
  const [inviting, setInviting] = useState(false);

  // pending sign-ups (only loaded when the current user may approve)
  const [pendingUsers, setPendingUsers] = useState<PendingUser[]>([]);
  const [canApprove, setCanApprove] = useState(false);
  const [pendingBusy, setPendingBusy] = useState<string | null>(null);

  const selectedOrg = useMemo(
    () => orgs.find((o) => o.id === selectedOrgId) ?? null,
    [orgs, selectedOrgId],
  );

  const refreshOrgs = useCallback(async () => {
    setError(null);
    try {
      const list = await apiJson<Org[]>('/api/orgs');
      setOrgs(list);
      if (list.length > 0 && !selectedOrgId) setSelectedOrgId(list[0].id);
      if (list.length === 0) setShowCreate(true);
    } catch (e) {
      setError(`Could not load organizations: ${(e as Error).message}`);
    }
  }, [selectedOrgId]);

  const refreshMembers = useCallback(async () => {
    if (!selectedOrgId) {
      setMembers([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const list = await apiJson<Member[]>(`/api/orgs/${selectedOrgId}/members`);
      setMembers(list);
    } catch (e) {
      setError(`Could not load members: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [selectedOrgId]);

  // Loads the approval queue. The endpoint 403s for non-approvers, which we
  // treat as "hide the section" rather than an error.
  const refreshPending = useCallback(async () => {
    try {
      const res = await fetch('/api/auth/pending-users', {
        headers: getAuthHeaders(),
      });
      if (res.status === 403) {
        setCanApprove(false);
        setPendingUsers([]);
        return;
      }
      if (!res.ok) throw new Error(`${res.status}`);
      const list = (await res.json()) as PendingUser[];
      setCanApprove(true);
      setPendingUsers(list);
    } catch {
      setCanApprove(false);
      setPendingUsers([]);
    }
  }, []);

  useEffect(() => {
    void refreshOrgs();
  }, [refreshOrgs]);

  useEffect(() => {
    void refreshMembers();
  }, [refreshMembers]);

  useEffect(() => {
    void refreshPending();
  }, [refreshPending]);

  async function handleApprove(u: PendingUser) {
    setPendingBusy(u.id);
    setError(null);
    try {
      await apiJson(`/api/auth/users/${u.id}/approve`, { method: 'POST' });
      await refreshPending();
    } catch (e) {
      setError(`Approve failed: ${(e as Error).message}`);
    } finally {
      setPendingBusy(null);
    }
  }

  async function handleReject(u: PendingUser) {
    if (!confirm(`Reject ${u.email}? They will no longer be able to log in.`)) return;
    setPendingBusy(u.id);
    setError(null);
    try {
      await apiJson(`/api/auth/users/${u.id}/reject`, { method: 'POST' });
      await refreshPending();
    } catch (e) {
      setError(`Reject failed: ${(e as Error).message}`);
    } finally {
      setPendingBusy(null);
    }
  }

  const canManage = selectedOrg && (selectedOrg.user_role === 'admin' || selectedOrg.user_role === 'owner');
  const isOwner = selectedOrg?.user_role === 'owner';

  async function handleCreateOrg(e: React.FormEvent) {
    e.preventDefault();
    if (!orgName.trim()) return;
    try {
      const created = await apiJson<Org>('/api/orgs', {
        method: 'POST',
        body: JSON.stringify({ name: orgName.trim() }),
      });
      setOrgName('');
      setShowCreate(false);
      await refreshOrgs();
      setSelectedOrgId(created.id);
    } catch (e) {
      setError(`Create failed: ${(e as Error).message}`);
    }
  }

  async function handleInvite(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedOrgId || !inviteEmail.trim()) return;
    setInviting(true);
    setError(null);
    try {
      await apiJson<Member>(`/api/orgs/${selectedOrgId}/members/invite`, {
        method: 'POST',
        body: JSON.stringify({ email: inviteEmail.trim(), role: inviteRole }),
      });
      setInviteEmail('');
      await refreshMembers();
    } catch (e) {
      setError(`Invite failed: ${(e as Error).message}`);
    } finally {
      setInviting(false);
    }
  }

  async function handleChangeRole(m: Member, newRole: Member['role']) {
    if (!selectedOrgId || m.role === newRole) return;
    try {
      await apiJson<Member>(`/api/orgs/${selectedOrgId}/members/${m.user_id}`, {
        method: 'PUT',
        body: JSON.stringify({ role: newRole }),
      });
      await refreshMembers();
      if (newRole === 'owner') await refreshOrgs();
    } catch (e) {
      setError(`Role change failed: ${(e as Error).message}`);
    }
  }

  async function handleRemove(m: Member) {
    if (!selectedOrgId) return;
    if (!confirm(`Remove ${m.email} from ${selectedOrg?.name}?`)) return;
    try {
      await apiJson<void>(`/api/orgs/${selectedOrgId}/members/${m.user_id}`, {
        method: 'DELETE',
      });
      await refreshMembers();
    } catch (e) {
      setError(`Remove failed: ${(e as Error).message}`);
    }
  }

  return (
    <div className="flex flex-col h-full bg-background">
      <header className="border-b border-border px-4 md:px-6 py-3 md:py-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <Building2 className="h-5 w-5 shrink-0" />
        <h1 className="text-xl font-semibold">Members</h1>
        <span className="hidden text-xs text-muted-foreground sm:inline">Organization access management</span>
        <div className="ml-auto flex items-center gap-2 sm:gap-3 text-sm">
          <select
            value={selectedOrgId ?? ''}
            onChange={(e) => setSelectedOrgId(e.target.value || null)}
            className="rounded-md border border-border bg-card px-3 py-1.5"
          >
            <option value="">(no organization)</option>
            {orgs.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name} — {o.user_role}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => setShowCreate((v) => !v)}
            className="px-3 py-1.5 rounded-md border border-border text-sm hover:bg-accent"
          >
            New org
          </button>
          <button
            type="button"
            onClick={() => {
              void refreshOrgs();
              void refreshMembers();
              void refreshPending();
            }}
            className="p-2 rounded-md border border-border hover:bg-accent"
            title="Refresh"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </header>

      <main className="flex-1 overflow-y-auto p-4 md:p-6 space-y-6">
        {error && (
          <div className="rounded-lg bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 text-sm">
            {error}
          </div>
        )}

        {canApprove && pendingUsers.length > 0 && (
          <section className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-5">
            <h2 className="font-semibold mb-1 flex items-center gap-2">
              <UserCheck className="h-4 w-4" /> Pending sign-ups ({pendingUsers.length})
            </h2>
            <p className="text-xs text-muted-foreground mb-3">
              These people registered and are waiting to be let in. Approving grants
              access to the shared workspace; rejecting blocks the account.
            </p>
            <div className="divide-y divide-border">
              {pendingUsers.map((u) => (
                <div key={u.id} className="flex items-center gap-4 py-3">
                  <div className="h-9 w-9 rounded-full bg-amber-500/20 text-amber-600 flex items-center justify-center text-sm font-semibold shrink-0">
                    {(u.name || u.email).slice(0, 2).toUpperCase()}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm truncate">{u.name || '(no name)'}</div>
                    <div className="text-xs text-muted-foreground truncate">{u.email}</div>
                  </div>
                  <button
                    type="button"
                    onClick={() => void handleApprove(u)}
                    disabled={pendingBusy === u.id}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-xs font-medium disabled:opacity-50"
                  >
                    <Check className="h-3.5 w-3.5" />
                    {pendingBusy === u.id ? 'Working…' : 'Approve'}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleReject(u)}
                    disabled={pendingBusy === u.id}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-border text-xs font-medium hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                  >
                    <X className="h-3.5 w-3.5" />
                    Reject
                  </button>
                </div>
              ))}
            </div>
          </section>
        )}

        {showCreate && (
          <section className="rounded-lg border border-border bg-card p-5">
            <h2 className="font-semibold mb-3">Create organization</h2>
            <form onSubmit={handleCreateOrg} className="flex gap-3">
              <input
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                placeholder="Acme Inc."
                className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
                required
              />
              <button
                type="submit"
                disabled={!orgName.trim()}
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
              >
                Create
              </button>
            </form>
            <p className="text-xs text-muted-foreground mt-2">
              You become the owner of the new organization.
            </p>
          </section>
        )}

        {selectedOrg && canManage && (
          <section className="rounded-lg border border-border bg-card p-5">
            <h2 className="font-semibold mb-3 flex items-center gap-2">
              <UserPlus className="h-4 w-4" /> Invite a member
            </h2>
            <form onSubmit={handleInvite} className="flex flex-wrap gap-3">
              <input
                type="email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                placeholder="user@example.com"
                className="flex-1 min-w-[220px] rounded-md border border-border bg-background px-3 py-2 text-sm"
                required
              />
              <select
                value={inviteRole}
                onChange={(e) => setInviteRole(e.target.value as Member['role'])}
                className="rounded-md border border-border bg-background px-3 py-2 text-sm"
              >
                {(['viewer', 'member', 'admin'] as const)
                  .filter((r) => isOwner || roleLevel(r) <= roleLevel(selectedOrg.user_role))
                  .map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
              </select>
              <button
                type="submit"
                disabled={inviting || !inviteEmail.trim()}
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
              >
                {inviting ? 'Inviting…' : 'Invite'}
              </button>
            </form>
            <p className="text-xs text-muted-foreground mt-2">
              The invitee must already have a registered account.
            </p>
          </section>
        )}

        <section className="rounded-lg border border-border bg-card">
          <div className="px-5 py-3 border-b border-border flex items-center justify-between">
            <h2 className="font-semibold">
              Members {selectedOrg ? `of ${selectedOrg.name}` : ''}
            </h2>
            <span className="text-xs text-muted-foreground">
              {loading ? 'loading…' : `${members.length} total`}
            </span>
          </div>
          <div className="divide-y divide-border">
            {members.length === 0 && !loading && (
              <div className="px-5 py-8 text-center text-sm text-muted-foreground">
                {selectedOrg ? 'No members yet.' : 'Select an organization.'}
              </div>
            )}
            {members.map((m) => (
              <div key={m.id} className="flex items-center gap-4 px-5 py-3">
                <div className="h-9 w-9 rounded-full bg-primary/20 text-primary flex items-center justify-center text-sm font-semibold shrink-0">
                  {(m.name || m.email).slice(0, 2).toUpperCase()}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm truncate">{m.name || '(no name)'}</div>
                  <div className="text-xs text-muted-foreground truncate">{m.email}</div>
                </div>
                {isOwner ? (
                  <select
                    value={m.role}
                    onChange={(e) => void handleChangeRole(m, e.target.value as Member['role'])}
                    className="rounded-md border border-border bg-background px-2 py-1 text-xs"
                  >
                    {ROLES.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="text-xs uppercase tracking-wider text-muted-foreground">
                    {m.role}
                  </span>
                )}
                {canManage && m.role !== 'owner' && (
                  <button
                    type="button"
                    onClick={() => void handleRemove(m)}
                    className="p-2 rounded-md hover:bg-destructive/10 text-destructive"
                    title="Remove"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
