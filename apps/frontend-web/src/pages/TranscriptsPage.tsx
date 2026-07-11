/**
 * Meeting transcripts — upload paste/file, list, view.
 * Uses /api/ext/transcripts POST + /api/ext/transcripts/{project} GET.
 *
 * Also overlays graphify status from /api/projects/{id}/docs/status so the
 * user can tell at a glance which transcripts are already in the knowledge
 * graph and which ones need a fresh `graphify extract`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Calendar,
  CheckCircle2,
  Eye,
  FileAudio,
  Loader2,
  Mic,
  Network,
  RefreshCw,
  Square,
  Upload,
  Users as UsersIcon,
} from 'lucide-react';

import { getAuthHeaders } from '../lib/auth';
import { useProjectStore } from '../stores/project-store';

type TranscriptMeta = {
  filename: string;
  title?: string;
  occurred_at?: string | null;
  participants?: string[] | null;
  source?: string | null;
  size?: number;
  modified?: string;
};

type DocsGraphStatus = {
  has_graph: boolean;
  last_graphify?: string | null;
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

type TranscribeJob = {
  id: string;
  status: string;
  progress?: number;
  text?: string | null;
  error?: string | null;
};

function fmtElapsed(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * Record from the mic (or pick an audio/video file) → send to the external
 * Transcriber service → poll → the finished transcript is saved server-side and
 * shows up in the list below (parent refreshes via onComplete).
 */
function AudioTranscribeSection({ slug, onComplete }: { slug: string; onComplete: () => void }) {
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [language, setLanguage] = useState('');
  const [job, setJob] = useState<TranscribeJob | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);

  const clearTimers = () => {
    if (timerRef.current) window.clearInterval(timerRef.current);
    if (pollRef.current) window.clearInterval(pollRef.current);
    timerRef.current = null;
    pollRef.current = null;
  };

  useEffect(() => () => {
    clearTimers();
    recorderRef.current?.stream?.getTracks().forEach((t) => t.stop());
  }, []);

  const pollJob = (id: string) => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      try {
        const s = await api<TranscribeJob>(
          `/api/ext/projects/${encodeURIComponent(slug)}/transcribe/${id}`,
        );
        setJob({ ...s, id });
        if (s.status === 'completed' || s.status === 'failed') {
          if (pollRef.current) window.clearInterval(pollRef.current);
          pollRef.current = null;
          if (s.status === 'completed') onComplete();
        }
      } catch {
        /* transient — keep polling */
      }
    }, 2000);
  };

  // Restore an in-progress job after navigating away and back: the backend
  // tracks jobs server-side, so re-hydrate the status card and resume polling.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api<{ jobs: Array<Record<string, unknown>> }>(
          `/api/ext/projects/${encodeURIComponent(slug)}/transcribe`,
        );
        if (cancelled) return;
        const active = (data.jobs || []).find((j) =>
          ['queued', 'processing', 'uploading'].includes(String(j.status)),
        );
        const jid = active ? String(active.job_id ?? active.id ?? '') : '';
        if (jid) {
          setJob({
            id: jid,
            status: String(active!.status),
            progress: typeof active!.progress === 'number' ? active!.progress : undefined,
          });
          pollJob(jid);
        }
      } catch {
        /* ignore — nothing to restore */
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  const submitBlob = async (blob: Blob, filename: string) => {
    setErr(null);
    setJob({ id: '', status: 'uploading' });
    const fd = new FormData();
    fd.append('audio', blob, filename);
    if (language.trim()) fd.append('language', language.trim());
    try {
      const res = await fetch(`/api/ext/projects/${encodeURIComponent(slug)}/transcribe`, {
        method: 'POST',
        headers: { ...getAuthHeaders() },
        body: fd,
      });
      const j = await res.json();
      if (!res.ok) throw new Error(typeof j.detail === 'string' ? j.detail : res.statusText);
      setJob({ id: j.job_id, status: j.status });
      pollJob(j.job_id);
    } catch (e) {
      setErr(`Upload failed: ${(e as Error).message}`);
      setJob(null);
    }
  };

  const startRecording = async () => {
    setErr(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      chunksRef.current = [];
      mr.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      mr.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const type = mr.mimeType || 'audio/webm';
        const blob = new Blob(chunksRef.current, { type });
        const ext = type.includes('ogg') ? 'ogg' : 'webm';
        const stamp = new Date().toISOString().replace(/[:.]/g, '-');
        void submitBlob(blob, `recording-${stamp}.${ext}`);
      };
      recorderRef.current = mr;
      mr.start();
      setRecording(true);
      setElapsed(0);
      timerRef.current = window.setInterval(() => setElapsed((e) => e + 1), 1000);
    } catch {
      setErr('Microphone access was denied or is unavailable in this browser.');
    }
  };

  const stopRecording = () => {
    recorderRef.current?.stop();
    setRecording(false);
    if (timerRef.current) window.clearInterval(timerRef.current);
    timerRef.current = null;
  };

  const busy = !!job && !['completed', 'failed'].includes(job.status);

  return (
    <section className="rounded-lg border border-border bg-card p-5">
      <h2 className="font-semibold mb-3 flex items-center gap-2">
        <Mic className="h-4 w-4" /> Record or transcribe audio
      </h2>
      <p className="text-xs text-muted-foreground mb-3">
        Record a meeting or upload an audio/video file. It’s transcribed on the GPU and saved below.
      </p>

      <div className="flex flex-wrap items-center gap-3">
        {!recording ? (
          <button
            onClick={() => void startRecording()}
            disabled={busy}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
          >
            <Mic className="h-4 w-4" /> Start recording
          </button>
        ) : (
          <button
            onClick={stopRecording}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-destructive text-destructive-foreground text-sm font-medium"
          >
            <Square className="h-4 w-4" /> Stop {fmtElapsed(elapsed)}
          </button>
        )}

        <label className="cursor-pointer inline-flex items-center gap-2 text-xs px-3 py-2 rounded-md border border-border hover:bg-accent">
          <Upload className="h-3.5 w-3.5" />
          Upload audio / video
          <input
            type="file"
            accept="audio/*,video/*,.mp3,.wav,.m4a,.ogg,.flac,.aac,.opus,.webm,.mp4,.mov,.mkv,.m4v"
            className="hidden"
            disabled={busy || recording}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void submitBlob(f, f.name);
              e.target.value = '';
            }}
          />
        </label>

        <input
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          placeholder="lang (auto)"
          title="Optional language code, e.g. en, ru, kk. Blank = auto-detect."
          className="w-28 rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
      </div>

      {recording && (
        <div className="mt-3 flex items-center gap-2 text-sm text-destructive">
          <span className="h-2.5 w-2.5 rounded-full bg-destructive animate-pulse" />
          Recording… {fmtElapsed(elapsed)}
        </div>
      )}

      {job && (
        <div className="mt-4 rounded-md border border-border bg-background px-4 py-3 text-sm">
          {job.status === 'uploading' && (
            <span className="inline-flex items-center gap-2 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Uploading…
            </span>
          )}
          {(job.status === 'queued' || job.status === 'processing') && (
            <div className="flex items-center gap-3">
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
              <span className="capitalize">{job.status}</span>
              {job.status === 'processing' && typeof job.progress === 'number' && (
                <div className="flex-1 h-1.5 rounded bg-muted overflow-hidden">
                  <div className="h-full bg-primary transition-all" style={{ width: `${job.progress}%` }} />
                </div>
              )}
            </div>
          )}
          {job.status === 'completed' && (
            <span className="inline-flex items-center gap-2 text-emerald-600 dark:text-emerald-400">
              <CheckCircle2 className="h-4 w-4" /> Transcription complete — saved below.
            </span>
          )}
          {job.status === 'failed' && (
            <span className="inline-flex items-center gap-2 text-destructive">
              <AlertTriangle className="h-4 w-4" /> Failed: {job.error || 'unknown error'}
            </span>
          )}
        </div>
      )}

      {err && <div className="mt-3 text-sm text-destructive">{err}</div>}
    </section>
  );
}

type ChatMsg = { role: 'user' | 'assistant'; content: string };

/**
 * Chat over a saved transcript: summarize, identify/label speakers, ask
 * questions. Backed by /api/ext/projects/{slug}/transcript-chat (claude --print).
 */
function TranscriptChat({ slug, filename }: { slug: string; filename: string }) {
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const ask = async (question: string) => {
    const q = question.trim();
    if (!q || loading) return;
    setErr(null);
    const history = messages;
    setMessages((m) => [...m, { role: 'user', content: q }]);
    setInput('');
    setLoading(true);
    try {
      const res = await api<{ answer: string }>(
        `/api/ext/projects/${encodeURIComponent(slug)}/transcript-chat`,
        { method: 'POST', body: JSON.stringify({ filename, question: q, history }) },
      );
      setMessages((m) => [...m, { role: 'assistant', content: res.answer || '(no answer)' }]);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="border-t border-border flex flex-col min-h-0">
      <div className="px-5 py-2 flex flex-wrap items-center gap-2 border-b border-border">
        <span className="text-xs font-medium text-muted-foreground mr-1">Ask about this transcript:</span>
        <button
          onClick={() => void ask('Summarize this transcript in a few bullet points.')}
          disabled={loading}
          className="text-xs px-2 py-1 rounded border border-border hover:bg-accent disabled:opacity-50"
        >
          Summarize
        </button>
        <button
          onClick={() => void ask('Identify the distinct speakers and what role each seems to play.')}
          disabled={loading}
          className="text-xs px-2 py-1 rounded border border-border hover:bg-accent disabled:opacity-50"
        >
          Identify speakers
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-3 space-y-3 min-h-0 max-h-64">
        {messages.length === 0 && (
          <p className="text-xs text-muted-foreground">
            Ask a question, or tell me who the speakers are (e.g. “speaker 1 is Alice”).
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'text-right' : ''}>
            <div
              className={
                'inline-block max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap text-left ' +
                (m.role === 'user'
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-muted text-foreground')
              }
            >
              {m.content}
            </div>
          </div>
        ))}
        {loading && (
          <div className="inline-flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Thinking…
          </div>
        )}
        {err && <div className="text-sm text-destructive">Error: {err}</div>}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void ask(input);
        }}
        className="px-5 py-3 border-t border-border flex items-center gap-2"
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about this transcript…"
          disabled={loading}
          className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
        >
          Ask
        </button>
      </form>
    </div>
  );
}

export function TranscriptsPage() {
  const projects = useProjectStore((s) => s.projects);
  const selectedProjectId = useProjectStore((s) => s.selectedProjectId);
  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const currentId = activeProjectId || selectedProjectId;
  const currentProject = projects.find((p) => p.id === currentId);
  const slug = useMemo(() => projectSlug(currentProject?.name, currentProject?.id), [currentProject]);

  const [list, setList] = useState<TranscriptMeta[]>([]);
  const [docsStatus, setDocsStatus] = useState<DocsGraphStatus | null>(null);
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
      setDocsStatus(null);
      return;
    }
    setLoading(true);
    setError(null);
    // Fetch transcripts and docs status in parallel. The status call is
    // best-effort — if it fails we still render the list, just without
    // graphify badges.
    const [listRes, statusRes] = await Promise.allSettled([
      api<TranscriptMeta[]>(`/api/ext/transcripts/${encodeURIComponent(slug)}`),
      api<DocsGraphStatus>(`/api/projects/${encodeURIComponent(currentProject.id)}/docs/status`),
    ]);
    if (listRes.status === 'fulfilled') {
      setList(listRes.value);
    } else {
      setError(`Could not load transcripts: ${(listRes.reason as Error).message}`);
    }
    setDocsStatus(statusRes.status === 'fulfilled' ? statusRes.value : null);
    setLoading(false);
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

  // A transcript is "pending" if it was written after the last graphify run
  // (or if there's no graph yet at all). The backend writes `modified` in
  // UTC and `last_graphify` in local-naive ISO — Date.parse handles both
  // shapes; small skew is acceptable for a "stale?" hint.
  const graphRunAt = docsStatus?.last_graphify ? Date.parse(docsStatus.last_graphify) : null;
  const isPending = (t: TranscriptMeta): boolean => {
    if (!docsStatus?.has_graph || graphRunAt === null || Number.isNaN(graphRunAt)) return true;
    if (!t.modified) return false;
    const modifiedAt = Date.parse(t.modified);
    return !Number.isNaN(modifiedAt) && modifiedAt > graphRunAt;
  };
  const pendingCount = list.filter(isPending).length;

  if (!currentProject) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        Select a project to manage transcripts.
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-background">
      <header className="border-b border-border px-4 md:px-6 py-3 md:py-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <FileAudio className="h-5 w-5 shrink-0" />
        <h1 className="text-lg md:text-xl font-semibold">Meeting Transcripts</h1>
        <span className="hidden text-xs text-muted-foreground sm:inline">
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

      <main className="flex-1 overflow-y-auto p-4 md:p-6 space-y-6">
        {error && (
          <div className="rounded-lg bg-destructive/10 border border-destructive/50 text-destructive px-4 py-3 text-sm">
            {error}
          </div>
        )}

        {docsStatus && list.length > 0 && (
          pendingCount > 0 ? (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300 px-4 py-3 text-sm flex items-start gap-3">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
              <div className="flex-1">
                <div className="font-medium">
                  {pendingCount} transcript{pendingCount === 1 ? '' : 's'} not yet in the knowledge graph
                </div>
                <div className="text-xs mt-1 opacity-90">
                  {docsStatus.has_graph && docsStatus.last_graphify ? (
                    <>Graph last refreshed {new Date(docsStatus.last_graphify).toLocaleString()}. </>
                  ) : (
                    <>No graph yet. </>
                  )}
                  Run <code className="px-1 py-0.5 rounded bg-amber-500/20 font-mono">/graphify</code> in Claude Code,
                  or <code className="px-1 py-0.5 rounded bg-amber-500/20 font-mono">graphify extract {currentProject.path}</code> from a shell, to include them.
                </div>
              </div>
            </div>
          ) : (
            <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 px-4 py-3 text-sm flex items-start gap-3">
              <Network className="h-4 w-4 mt-0.5 shrink-0" />
              <div className="flex-1">
                <div className="font-medium">All transcripts are in the knowledge graph</div>
                {docsStatus.last_graphify && (
                  <div className="text-xs mt-1 opacity-90">
                    Last refreshed {new Date(docsStatus.last_graphify).toLocaleString()}.
                  </div>
                )}
              </div>
            </div>
          )
        )}

        <AudioTranscribeSection slug={slug} onComplete={() => void refresh()} />

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
                  <div className="flex items-center gap-2">
                    <div className="font-medium text-sm truncate">{t.title || t.filename}</div>
                    {docsStatus && (
                      isPending(t) ? (
                        <span
                          className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 text-amber-700 dark:text-amber-300 px-2 py-0.5 text-[10px] font-medium"
                          title="This transcript is not yet folded into graphify's knowledge graph"
                        >
                          <span className="h-1.5 w-1.5 rounded-full bg-amber-500" /> Pending
                        </span>
                      ) : (
                        <span
                          className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 px-2 py-0.5 text-[10px] font-medium"
                          title="This transcript is in graphify's knowledge graph"
                        >
                          <CheckCircle2 className="h-3 w-3" /> In graph
                        </span>
                      )
                    )}
                  </div>
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
              <pre className="overflow-auto px-5 py-4 text-xs font-mono whitespace-pre-wrap max-h-[40vh] shrink-0">
                {viewing.content}
              </pre>
              <TranscriptChat slug={slug} filename={viewing.filename} />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
