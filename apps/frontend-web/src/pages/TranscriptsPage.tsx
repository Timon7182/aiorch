/**
 * Meeting transcripts — upload paste/file, list, view.
 * Uses /api/ext/transcripts POST + /api/ext/transcripts/{project} GET.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { FileAudio, Upload, RefreshCw, Eye, Calendar, Users as UsersIcon } from 'lucide-react';

import { getAuthHeaders } from '../lib/auth';
import { useProjectStore } from '../stores/project-store';

type TranscriptMeta = {
  filename: string;
  title: string;
  occurred_at?: string | null;
  participants?: string[] | null;
  source?: string | null;
  size?: number;
};

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { ...getAuthHeaders(), 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const j = (await res.json()) as { detail?: unknown };
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

function projectSlug(name: string | undefined, id: string | undefined): string {
  const raw = (name || id || '').toLowerCase().replace(/[^a-z0-9-_]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
  return raw || 'default';
}

export function TranscriptsPage() {
  const projects = useProjectStore((s) => s.projects);
  const selectedProjectId = useProjectStore((s) => s.selectedProjectId);
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const currentId = activeProjectId || selectedProjectId;
  const currentProject = projects.find((p) => p.id === currentId);
  const slug = useMemo(() => projectSlug(currentProject?.name, currentProject?.id), [currentProject]);

  const [list, setList] = useState<TranscriptMeta[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewing, setViewing] = useState<{ filename: string; content: string } | null>(null);

  // upload form
  const [title, setTitle] = useState('');
  const [occurredAt, setOccurredAt] = useState('');
  const [participants, setParticipants] = useState('');
  const [content, setContent] = useState('');
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    if (!currentProject) {
      setList([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api<TranscriptMeta[]>(`/api/ext/transcripts/${encodeURIComponent(slug)}`);
      setList(data);
    } catch (e) {
      setError(`Could not load transcripts: ${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [currentProject, slug]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleFile = (file: File) => {
    if (!title) setTitle(file.name.replace(/\.[^.]+$/, ''));
    const reader = new FileReader();
    reader.onload = () => setContent(String(reader.result || ''));
    reader.readAsText(file);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!currentProject || !title.trim() || !content.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const parts = participants.split(',').map((s) => s.trim()).filter(Boolean);
      await api<unknown>('/api/ext/transcripts', {
        method: 'POST',
        body: JSON.stringify({
          project: slug,
          title: title.trim(),
          content,
          occurred_at: occurredAt || null,
          participants: parts.length > 0 ? parts : null,
          source: 'manual-paste',
        }),
      });
      setTitle('');
      setOccurredAt('');
      setParticipants('');
      setContent('');
      await refresh();
    } catch (e) {
      setError(`Save failed: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const view = async (filename: string) => {
    try {
      const data = await api<{ filename: string; content: string }>(
        `/api/ext/transcripts/${encodeURIComponent(slug)}/${encodeURIComponent(filename)}`,
      );
      setViewing(data);
    } catch (e) {
      setError(`Open failed: ${(e as Error).message}`);
    }
  };

  if (!currentProject) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        Select a project to manage transcripts.
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center gap-4">
        <FileAudio className="h-5 w-5" />
        <h1 className="text-xl font-semibold">Meeting Transcripts</h1>
        <span className="text-xs text-muted-foreground">
          {currentProject.name} — indexed for Hermes citations
        </span>
        <button
          onClick={() => void refresh()}
          className="ml-auto p-2 rounded-md border border-border hover:bg-accent"
          title="Refresh"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </header>

      <main className="flex-1 overflow-y-auto p-6 space-y-6">
        {error && (
          <div className="rounded-lg bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 text-sm">
            {error}
          </div>
        )}

        <section className="rounded-lg border border-border bg-card p-5">
          <h2 className="font-semibold mb-3 flex items-center gap-2">
            <Upload className="h-4 w-4" /> Add transcript
          </h2>
          <form onSubmit={submit} className="space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-medium mb-1">Title</label>
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Weekly sync — Mar 14"
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                  required
                />
              </div>
              <div>
                <label className="block text-xs font-medium mb-1">Occurred at</label>
                <input
                  type="datetime-local"
                  value={occurredAt}
                  onChange={(e) => setOccurredAt(e.target.value)}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium mb-1">Participants (comma-separated)</label>
              <input
                value={participants}
                onChange={(e) => setParticipants(e.target.value)}
                placeholder="Alice, Bob, Carol"
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-medium mb-1">Content</label>
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder="Paste transcript text here, or use the upload button below…"
                rows={8}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono resize-y"
                required
              />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="cursor-pointer inline-flex items-center gap-2 text-xs px-3 py-2 rounded-md border border-border hover:bg-accent">
                <Upload className="h-3.5 w-3.5" />
                Upload .txt / .md / .vtt
                <input
                  type="file"
                  accept=".txt,.md,.vtt,.srt"
                  className="hidden"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) handleFile(f);
                  }}
                />
              </label>
              <button
                type="submit"
                disabled={saving || !title.trim() || !content.trim()}
                className="ml-auto px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save transcript'}
              </button>
            </div>
          </form>
        </section>

        <section className="rounded-lg border border-border bg-card">
          <div className="px-5 py-3 border-b border-border flex items-center justify-between">
            <h2 className="font-semibold">Stored transcripts</h2>
            <span className="text-xs text-muted-foreground">
              {loading ? 'loading…' : `${list.length} total`}
            </span>
          </div>
          <div className="divide-y divide-border">
            {list.length === 0 && !loading && (
              <div className="px-5 py-8 text-center text-sm text-muted-foreground">
                No transcripts yet. Paste one above to get started.
              </div>
            )}
            {list.map((t) => (
              <div key={t.filename} className="flex items-center gap-4 px-5 py-3">
                <FileAudio className="h-4 w-4 text-muted-foreground shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm truncate">{t.title || t.filename}</div>
                  <div className="text-xs text-muted-foreground flex flex-wrap items-center gap-2">
                    {t.occurred_at && (
                      <span className="inline-flex items-center gap-1">
                        <Calendar className="h-3 w-3" /> {t.occurred_at}
                      </span>
                    )}
                    {t.participants && t.participants.length > 0 && (
                      <span className="inline-flex items-center gap-1">
                        <UsersIcon className="h-3 w-3" /> {t.participants.join(', ')}
                      </span>
                    )}
                    {t.source && <span className="opacity-60">via {t.source}</span>}
                  </div>
                </div>
                <button
                  onClick={() => void view(t.filename)}
                  className="p-2 rounded-md hover:bg-accent"
                  title="View"
                >
                  <Eye className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        </section>

        {viewing && (
          <div
            className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
            onClick={() => setViewing(null)}
          >
            <div
              className="bg-card border border-border rounded-lg max-w-3xl w-full max-h-[85vh] flex flex-col"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="px-5 py-3 border-b border-border flex items-center justify-between">
                <h3 className="font-semibold truncate">{viewing.filename}</h3>
                <button
                  onClick={() => setViewing(null)}
                  className="px-3 py-1 text-xs rounded border border-border hover:bg-accent"
                >
                  Close
                </button>
              </div>
              <pre className="flex-1 overflow-auto px-5 py-4 text-xs font-mono whitespace-pre-wrap">
                {viewing.content}
              </pre>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
