/**
 * Admin page — global user & access management (admin-only screen).
 *
 * Not scoped to any project. Lets a global admin:
 *  - Users tab:       create users, change role/status, reset/generate password, delete
 *  - Access tab:      grant a user access to specific projects and pages within them
 *  - Integration tab: configure JIRA / Atlassian sync (stored now, sync wired up later)
 *
 * Reachable from the sidebar "Admin" item (rendered only for role==='admin')
 * at /admin. If a non-admin lands here, the API 403s and we show a notice.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ShieldCheck, Users, FolderLock, Plug, RefreshCw, UserPlus, Trash2,
  KeyRound, Copy, Check, X, Info,
} from 'lucide-react';

import { getAuthHeaders } from '../lib/auth';
import { useAuthStore } from '../stores/auth-store';

type AdminTab = 'users' | 'access' | 'integration';

type AdminUser = {
  id: string;
  email: string;
  name: string;
  role: string;       // 'user' | 'admin'
  status: string;     // 'pending' | 'active'
  is_active: boolean;
  created_at: string;
};

type ProjectLite = { id: string; name: string; path: string };
type PageDef = { id: string; label: string };
type Grant = { project_id: string; pages: string[] | null };
type AccessResponse = { user_id: string; unrestricted: boolean; grants: Grant[] };

type JiraConfig = {
  enabled: boolean;
  base_url: string | null;
  email: string | null;
  api_token: string | null;
  project_key: string | null;
  jql: string | null;
};

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

const inputCls =
  'rounded-md border border-border bg-background px-3 py-2 text-sm';
const btnPrimary =
  'px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50';
const btnGhost =
  'px-3 py-1.5 rounded-md border border-border text-sm hover:bg-accent disabled:opacity-50';

export function AdminPage() {
  const currentUser = useAuthStore((s) => s.user);
  const [tab, setTab] = useState<AdminTab>('users');
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  if (currentUser && currentUser.role !== 'admin') {
    return (
      <div className="flex h-full items-center justify-center bg-background">
        <div className="max-w-sm text-center text-muted-foreground">
          <ShieldCheck className="mx-auto mb-3 h-8 w-8" />
          <p className="font-medium text-foreground">Admins only</p>
          <p className="text-sm">You do not have access to this screen.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-background">
      <header className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3 md:px-6 md:py-4">
        <ShieldCheck className="h-5 w-5 shrink-0" />
        <h1 className="text-xl font-semibold">Administration</h1>
        <span className="hidden text-xs text-muted-foreground sm:inline">
          Users, access control &amp; integrations
        </span>
        <nav className="ml-auto flex items-center gap-1 rounded-lg bg-muted/40 p-1 text-sm">
          {([
            ['users', 'Users', Users],
            ['access', 'Access', FolderLock],
            ['integration', 'Integrations', Plug],
          ] as const).map(([id, label, Icon]) => (
            <button
              key={id}
              type="button"
              onClick={() => { setTab(id); setError(null); }}
              className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 ${
                tab === id ? 'bg-background shadow-sm font-medium' : 'text-muted-foreground'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </nav>
      </header>

      <main className="flex-1 overflow-y-auto p-4 md:p-6">
        {forbidden && (
          <div className="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            This account is not a global admin. Promote it with{' '}
            <code className="rounded bg-background px-1">python make_admin.py {currentUser?.email}</code>.
          </div>
        )}
        {error && (
          <div className="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {tab === 'users' && (
          <UsersTab onError={setError} onForbidden={() => setForbidden(true)} selfId={currentUser?.id} />
        )}
        {tab === 'access' && <AccessTab onError={setError} />}
        {tab === 'integration' && <IntegrationTab onError={setError} />}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Users tab
// ---------------------------------------------------------------------------

function UsersTab({
  onError, onForbidden, selfId,
}: {
  onError: (e: string | null) => void;
  onForbidden: () => void;
  selfId?: string;
}) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(false);

  // create form
  const [showCreate, setShowCreate] = useState(false);
  const [cEmail, setCEmail] = useState('');
  const [cName, setCName] = useState('');
  const [cPassword, setCPassword] = useState('');
  const [cRole, setCRole] = useState<'user' | 'admin'>('user');
  const [creating, setCreating] = useState(false);

  // password reset result
  const [resetFor, setResetFor] = useState<AdminUser | null>(null);
  const [resetPassword, setResetPassword] = useState('');
  const [generated, setGenerated] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    onError(null);
    try {
      const list = await apiJson<AdminUser[]>('/api/admin/users');
      setUsers(list);
    } catch (e) {
      const msg = (e as Error).message;
      if (msg.includes('403') || msg.toLowerCase().includes('admin')) onForbidden();
      onError(`Could not load users: ${msg}`);
    } finally {
      setLoading(false);
    }
  }, [onError, onForbidden]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    onError(null);
    try {
      await apiJson<AdminUser>('/api/admin/users', {
        method: 'POST',
        body: JSON.stringify({ email: cEmail.trim(), name: cName.trim(), password: cPassword, role: cRole }),
      });
      setCEmail(''); setCName(''); setCPassword(''); setCRole('user'); setShowCreate(false);
      await refresh();
    } catch (e) {
      onError(`Create failed: ${(e as Error).message}`);
    } finally {
      setCreating(false);
    }
  }

  async function patchUser(u: AdminUser, body: Partial<Pick<AdminUser, 'role' | 'status' | 'is_active'>>) {
    onError(null);
    try {
      await apiJson<AdminUser>(`/api/admin/users/${u.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      await refresh();
    } catch (e) {
      onError(`Update failed: ${(e as Error).message}`);
    }
  }

  async function handleDelete(u: AdminUser) {
    if (!confirm(`Permanently delete ${u.email}? This cannot be undone.`)) return;
    onError(null);
    try {
      await apiJson<void>(`/api/admin/users/${u.id}`, { method: 'DELETE' });
      await refresh();
    } catch (e) {
      onError(`Delete failed: ${(e as Error).message}`);
    }
  }

  function openReset(u: AdminUser) {
    setResetFor(u); setResetPassword(''); setGenerated(null); setCopied(false);
  }

  async function submitReset(generate: boolean) {
    if (!resetFor) return;
    onError(null);
    try {
      const res = await apiJson<{ message: string; generated_password: string | null }>(
        `/api/admin/users/${resetFor.id}/password`,
        { method: 'POST', body: JSON.stringify(generate ? {} : { password: resetPassword }) },
      );
      if (res.generated_password) {
        setGenerated(res.generated_password);
      } else {
        setResetFor(null);
      }
    } catch (e) {
      onError(`Password reset failed: ${(e as Error).message}`);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold">Users ({users.length})</h2>
        <div className="flex items-center gap-2">
          <button type="button" className={btnGhost} onClick={() => setShowCreate((v) => !v)}>
            <UserPlus className="mr-1.5 inline h-3.5 w-3.5" /> New user
          </button>
          <button type="button" className={btnGhost} onClick={() => void refresh()} title="Refresh">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {showCreate && (
        <section className="rounded-lg border border-border bg-card p-5">
          <h3 className="mb-3 font-semibold">Create user</h3>
          <form onSubmit={handleCreate} className="grid gap-3 sm:grid-cols-2">
            <input className={inputCls} placeholder="Full name" value={cName}
              onChange={(e) => setCName(e.target.value)} required />
            <input className={inputCls} type="email" placeholder="user@example.com" value={cEmail}
              onChange={(e) => setCEmail(e.target.value)} required />
            <input className={inputCls} type="text" placeholder="Initial password (min 8 chars)"
              value={cPassword} onChange={(e) => setCPassword(e.target.value)} minLength={8} required />
            <select className={inputCls} value={cRole} onChange={(e) => setCRole(e.target.value as 'user' | 'admin')}>
              <option value="user">user</option>
              <option value="admin">admin</option>
            </select>
            <div className="sm:col-span-2">
              <button type="submit" className={btnPrimary} disabled={creating}>
                {creating ? 'Creating…' : 'Create user'}
              </button>
              <span className="ml-3 text-xs text-muted-foreground">
                The account is created active (no approval needed).
              </span>
            </div>
          </form>
        </section>
      )}

      <section className="rounded-lg border border-border bg-card">
        <div className="divide-y divide-border">
          {users.length === 0 && !loading && (
            <div className="px-5 py-8 text-center text-sm text-muted-foreground">No users yet.</div>
          )}
          {users.map((u) => (
            <div key={u.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/20 text-sm font-semibold text-primary">
                {(u.name || u.email).slice(0, 2).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium">
                  {u.name || '(no name)'}
                  {u.id === selfId && <span className="ml-2 text-xs text-muted-foreground">(you)</span>}
                </div>
                <div className="truncate text-xs text-muted-foreground">{u.email}</div>
              </div>

              <select
                value={u.role}
                disabled={u.id === selfId}
                onChange={(e) => void patchUser(u, { role: e.target.value as 'user' | 'admin' })}
                className="rounded-md border border-border bg-background px-2 py-1 text-xs"
                title="Role"
              >
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>

              <select
                value={u.is_active ? u.status : 'disabled'}
                disabled={u.id === selfId}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === 'disabled') void patchUser(u, { is_active: false });
                  else void patchUser(u, { is_active: true, status: v as 'active' | 'pending' });
                }}
                className="rounded-md border border-border bg-background px-2 py-1 text-xs"
                title="Status"
              >
                <option value="active">active</option>
                <option value="pending">pending</option>
                <option value="disabled">disabled</option>
              </select>

              <button type="button" className={btnGhost} onClick={() => openReset(u)} title="Reset password">
                <KeyRound className="h-3.5 w-3.5" />
              </button>
              {u.id !== selfId && (
                <button
                  type="button"
                  onClick={() => void handleDelete(u)}
                  className="rounded-md p-2 text-destructive hover:bg-destructive/10"
                  title="Delete user"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          ))}
        </div>
      </section>

      {/* Password reset modal */}
      {resetFor && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-lg border border-border bg-card p-5 shadow-xl">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="flex items-center gap-2 font-semibold">
                <KeyRound className="h-4 w-4" /> Reset password
              </h3>
              <button type="button" onClick={() => setResetFor(null)} className="text-muted-foreground hover:text-foreground">
                <X className="h-4 w-4" />
              </button>
            </div>
            <p className="mb-3 text-sm text-muted-foreground">
              For <span className="font-medium text-foreground">{resetFor.email}</span>
            </p>

            {generated ? (
              <div className="space-y-3">
                <div className="rounded-md border border-border bg-muted/40 p-3">
                  <div className="mb-1 text-xs text-muted-foreground">New password (shown once — copy it now):</div>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 select-all break-all rounded bg-background px-2 py-1 text-sm">{generated}</code>
                    <button
                      type="button"
                      className={btnGhost}
                      onClick={() => { void navigator.clipboard.writeText(generated); setCopied(true); }}
                    >
                      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                    </button>
                  </div>
                </div>
                <button type="button" className={btnPrimary} onClick={() => setResetFor(null)}>Done</button>
              </div>
            ) : (
              <div className="space-y-3">
                <input
                  className={`${inputCls} w-full`}
                  type="text"
                  placeholder="New password (min 8 chars)"
                  value={resetPassword}
                  onChange={(e) => setResetPassword(e.target.value)}
                  minLength={8}
                />
                <div className="flex gap-2">
                  <button
                    type="button"
                    className={btnPrimary}
                    disabled={resetPassword.length < 8}
                    onClick={() => void submitReset(false)}
                  >
                    Set password
                  </button>
                  <button type="button" className={btnGhost} onClick={() => void submitReset(true)}>
                    Generate random
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Access tab
// ---------------------------------------------------------------------------

type GrantState = { granted: boolean; allPages: boolean; pages: Set<string> };

function AccessTab({ onError }: { onError: (e: string | null) => void }) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [projects, setProjects] = useState<ProjectLite[]>([]);
  const [pages, setPages] = useState<PageDef[]>([]);
  const [selectedUserId, setSelectedUserId] = useState<string>('');
  const [state, setState] = useState<Record<string, GrantState>>({});
  const [unrestricted, setUnrestricted] = useState(true);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const selectedUser = useMemo(
    () => users.find((u) => u.id === selectedUserId) ?? null,
    [users, selectedUserId],
  );

  // Load users + catalog once.
  useEffect(() => {
    (async () => {
      onError(null);
      try {
        const [u, p, pg] = await Promise.all([
          apiJson<AdminUser[]>('/api/admin/users'),
          apiJson<ProjectLite[]>('/api/admin/projects'),
          apiJson<PageDef[]>('/api/admin/pages'),
        ]);
        setUsers(u);
        setProjects(p);
        setPages(pg);
      } catch (e) {
        onError(`Could not load access data: ${(e as Error).message}`);
      }
    })();
  }, [onError]);

  // Load grants when a user is picked.
  const loadGrants = useCallback(async (userId: string) => {
    setLoading(true);
    onError(null);
    try {
      const res = await apiJson<AccessResponse>(`/api/admin/users/${userId}/access`);
      setUnrestricted(res.unrestricted);
      const next: Record<string, GrantState> = {};
      for (const g of res.grants) {
        next[g.project_id] = {
          granted: true,
          allPages: g.pages === null,
          pages: new Set(g.pages ?? []),
        };
      }
      setState(next);
    } catch (e) {
      onError(`Could not load grants: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    if (selectedUserId) void loadGrants(selectedUserId);
  }, [selectedUserId, loadGrants]);

  function get(projectId: string): GrantState {
    return state[projectId] ?? { granted: false, allPages: true, pages: new Set() };
  }
  function update(projectId: string, patch: Partial<GrantState>) {
    setState((prev) => ({ ...prev, [projectId]: { ...get(projectId), ...patch } }));
  }
  function togglePage(projectId: string, pageId: string) {
    const cur = get(projectId);
    const pages = new Set(cur.pages);
    if (pages.has(pageId)) pages.delete(pageId); else pages.add(pageId);
    update(projectId, { pages, allPages: false });
  }

  async function handleSave() {
    if (!selectedUserId) return;
    setSaving(true);
    onError(null);
    try {
      const grants: Grant[] = projects
        .filter((p) => get(p.id).granted)
        .map((p) => {
          const g = get(p.id);
          return { project_id: p.id, pages: g.allPages ? null : Array.from(g.pages) };
        });
      const res = await apiJson<AccessResponse>(`/api/admin/users/${selectedUserId}/access`, {
        method: 'PUT',
        body: JSON.stringify({ grants }),
      });
      setUnrestricted(res.unrestricted);
    } catch (e) {
      onError(`Save failed: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  const grantedCount = projects.filter((p) => get(p.id).granted).length;
  const isAdminUser = selectedUser?.role === 'admin';

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-semibold">Project &amp; page access</h2>
        <select
          value={selectedUserId}
          onChange={(e) => setSelectedUserId(e.target.value)}
          className={`${inputCls} ml-auto`}
        >
          <option value="">Select a user…</option>
          {users.map((u) => (
            <option key={u.id} value={u.id}>{u.name || u.email} — {u.role}</option>
          ))}
        </select>
      </div>

      {!selectedUserId && (
        <div className="rounded-lg border border-dashed border-border px-5 py-10 text-center text-sm text-muted-foreground">
          Pick a user to manage which projects and pages they can see.
        </div>
      )}

      {selectedUserId && (
        <>
          {isAdminUser && (
            <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm">
              <Info className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              <span>This user is a global admin and always has access to every project and page. Grants below are ignored while they remain an admin.</span>
            </div>
          )}
          {!isAdminUser && (
            <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
              <Info className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                {unrestricted && grantedCount === 0
                  ? 'No grants yet — this user can currently see ALL projects. Grant one or more projects below to restrict them to only those.'
                  : 'This user is restricted to the granted projects/pages below. Clear all grants to give them access to everything again.'}
              </span>
            </div>
          )}

          <section className="space-y-2">
            {projects.length === 0 && (
              <div className="rounded-lg border border-border px-5 py-8 text-center text-sm text-muted-foreground">
                No projects registered yet.
              </div>
            )}
            {projects.map((p) => {
              const g = get(p.id);
              return (
                <div key={p.id} className="rounded-lg border border-border bg-card p-4">
                  <label className="flex cursor-pointer items-center gap-3">
                    <input
                      type="checkbox"
                      checked={g.granted}
                      onChange={(e) => update(p.id, { granted: e.target.checked })}
                      className="h-4 w-4"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">{p.name}</div>
                      <div className="truncate text-[11px] text-muted-foreground" title={p.path}>{p.path}</div>
                    </div>
                  </label>

                  {g.granted && (
                    <div className="mt-3 border-t border-border pt-3 pl-7">
                      <label className="mb-2 flex w-fit cursor-pointer items-center gap-2 text-xs">
                        <input
                          type="checkbox"
                          checked={g.allPages}
                          onChange={(e) => update(p.id, { allPages: e.target.checked })}
                          className="h-3.5 w-3.5"
                        />
                        <span className="font-medium">All pages</span>
                      </label>
                      {!g.allPages && (
                        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
                          {pages.map((pg) => (
                            <label key={pg.id} className="flex cursor-pointer items-center gap-2 text-xs">
                              <input
                                type="checkbox"
                                checked={g.pages.has(pg.id)}
                                onChange={() => togglePage(p.id, pg.id)}
                                className="h-3.5 w-3.5"
                              />
                              {pg.label}
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </section>

          <div className="flex items-center gap-3">
            <button type="button" className={btnPrimary} onClick={() => void handleSave()} disabled={saving || loading}>
              {saving ? 'Saving…' : 'Save access'}
            </button>
            <span className="text-xs text-muted-foreground">{grantedCount} project(s) granted</span>
          </div>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Integration tab (JIRA / Atlassian)
// ---------------------------------------------------------------------------

function IntegrationTab({ onError }: { onError: (e: string | null) => void }) {
  const [cfg, setCfg] = useState<JiraConfig>({
    enabled: false, base_url: '', email: '', api_token: '', project_key: '', jql: '',
  });
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    (async () => {
      onError(null);
      try {
        const c = await apiJson<JiraConfig>('/api/admin/integrations/jira');
        setCfg({
          enabled: c.enabled ?? false,
          base_url: c.base_url ?? '',
          email: c.email ?? '',
          api_token: c.api_token ?? '',
          project_key: c.project_key ?? '',
          jql: c.jql ?? '',
        });
      } catch (e) {
        onError(`Could not load JIRA config: ${(e as Error).message}`);
      }
    })();
  }, [onError]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaved(false);
    onError(null);
    try {
      const c = await apiJson<JiraConfig>('/api/admin/integrations/jira', {
        method: 'PUT',
        body: JSON.stringify(cfg),
      });
      setCfg((prev) => ({ ...prev, ...c, api_token: c.api_token ?? prev.api_token }));
      setSaved(true);
    } catch (e) {
      onError(`Save failed: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="max-w-2xl space-y-5">
      <div className="flex items-start gap-2 rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
        <Info className="mt-0.5 h-4 w-4 shrink-0" />
        <span>
          JIRA / Atlassian sync is not active yet. You can store the connection
          settings here now; the sync job will use them once it ships. See the
          setup steps below.
        </span>
      </div>

      <form onSubmit={handleSave} className="space-y-4 rounded-lg border border-border bg-card p-5">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-semibold"><Plug className="h-4 w-4" /> JIRA connection</h2>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={cfg.enabled}
              onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
              className="h-4 w-4"
            />
            Enabled
          </label>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-2">
            Base URL
            <input className={inputCls} placeholder="https://your-domain.atlassian.net"
              value={cfg.base_url ?? ''} onChange={(e) => setCfg({ ...cfg, base_url: e.target.value })} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Account email
            <input className={inputCls} type="email" placeholder="you@company.com"
              value={cfg.email ?? ''} onChange={(e) => setCfg({ ...cfg, email: e.target.value })} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            API token
            <input className={inputCls} type="password" placeholder="Atlassian API token"
              value={cfg.api_token ?? ''} onChange={(e) => setCfg({ ...cfg, api_token: e.target.value })} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Project key
            <input className={inputCls} placeholder="ENG"
              value={cfg.project_key ?? ''} onChange={(e) => setCfg({ ...cfg, project_key: e.target.value })} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            JQL filter (optional)
            <input className={inputCls} placeholder="status != Done ORDER BY created DESC"
              value={cfg.jql ?? ''} onChange={(e) => setCfg({ ...cfg, jql: e.target.value })} />
          </label>
        </div>

        <div className="flex items-center gap-3">
          <button type="submit" className={btnPrimary} disabled={saving}>
            {saving ? 'Saving…' : 'Save configuration'}
          </button>
          {saved && <span className="flex items-center gap-1 text-xs text-green-600"><Check className="h-3.5 w-3.5" /> Saved</span>}
        </div>
      </form>

      <section className="rounded-lg border border-border bg-card p-5 text-sm">
        <h2 className="mb-3 font-semibold">How to connect JIRA (when sync ships)</h2>
        <ol className="list-decimal space-y-2 pl-5 text-muted-foreground">
          <li>In Atlassian, go to <span className="font-medium text-foreground">Account settings → Security → API tokens</span> and create a token.</li>
          <li>Set <span className="font-medium text-foreground">Base URL</span> to your site, e.g. <code className="rounded bg-muted px-1">https://your-domain.atlassian.net</code>.</li>
          <li>Enter the <span className="font-medium text-foreground">account email</span> the token belongs to and paste the <span className="font-medium text-foreground">API token</span>.</li>
          <li>Set the <span className="font-medium text-foreground">project key</span> (e.g. <code className="rounded bg-muted px-1">ENG</code>) to scope sync to one JIRA project. Optionally add a <span className="font-medium text-foreground">JQL</span> filter.</li>
          <li>Toggle <span className="font-medium text-foreground">Enabled</span> and save. The sync job will then import issues as tasks and push status changes back.</li>
        </ol>
        <p className="mt-3 text-xs text-muted-foreground">
          Developer note: implement the sync against the saved config in a new
          backend module (e.g. <code className="rounded bg-muted px-1">integrations/jira/</code>) reading
          <code className="rounded bg-muted px-1">IntegrationSetting(key="jira")</code>. See
          <code className="rounded bg-muted px-1">guides/integrations/jira.md</code>.
        </p>
      </section>
    </div>
  );
}
