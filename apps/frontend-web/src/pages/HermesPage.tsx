/**
 * Hermes chat page — routes each question through the Hermes service which
 * picks an LLM by intent, grounds it with project docs-index hits, and
 * streams the answer back over SSE.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRightCircle } from 'lucide-react';

import { getAuthHeaders } from '../lib/auth';

type Project = { id: string; slug: string; name: string };

function slugify(s: string): string {
  return (s || '').toLowerCase().replace(/[^a-z0-9-_]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
}

type ChatEvent =
  | { type: 'routing'; intent: string; model: string }
  | { type: 'citations'; value: Array<{ file_path: string; heading: string; line_start: number; snippet: string }> }
  | { type: 'token'; value: string }
  | { type: 'error'; value: string }
  | { type: 'done' };

type ChatTurn = {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  routing?: { intent: string; model: string };
  citations?: ChatEvent extends infer E ? (E extends { type: 'citations'; value: infer V } ? V : never) : never;
  error?: string;
};

export function HermesPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [handingOff, setHandingOff] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const turnIdRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const selectedProject = useMemo(
    () => projects.find((p) => p.slug === selectedSlug) ?? null,
    [projects, selectedSlug],
  );

  // Load the project list once.
  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch('/api/projects', { headers: getAuthHeaders() });
        if (!res.ok) return;
        const data = (await res.json()) as { projects?: Array<{ id: string; name: string; slug?: string }> } | Array<{ id: string; name: string; slug?: string }>;
        const raw = Array.isArray(data) ? data : data?.projects ?? [];
        const list: Project[] = raw.map((p) => ({
          id: p.id,
          name: p.name,
          slug: p.slug || slugify(p.name) || p.id,
        }));
        setProjects(list);
        if (list.length > 0 && !selectedSlug) setSelectedSlug(list[0].slug);
      } catch {
        /* leave empty */
      }
    })();
  }, [selectedSlug]);

  // Auto-scroll the conversation.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [turns]);

  const sendDisabled = useMemo(
    () => streaming || !input.trim(),
    [streaming, input],
  );

  async function send() {
    if (sendDisabled) return;
    const userTurn: ChatTurn = {
      id: ++turnIdRef.current,
      role: 'user',
      text: input.trim(),
    };
    const assistantTurn: ChatTurn = {
      id: ++turnIdRef.current,
      role: 'assistant',
      text: '',
    };
    setTurns((t) => [...t, userTurn, assistantTurn]);
    setInput('');
    setStreaming(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch('/api/ext/hermes/chat', {
        method: 'POST',
        headers: {
          ...getAuthHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ query: userTurn.text, project: selectedSlug }),
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`hermes ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          if (!block.startsWith('data:')) continue;
          const payload = block.slice('data:'.length).trim();
          if (!payload || payload === '[DONE]') continue;
          let event: ChatEvent;
          try {
            event = JSON.parse(payload) as ChatEvent;
          } catch {
            continue;
          }
          setTurns((cur) =>
            cur.map((t) =>
              t.id !== assistantTurn.id
                ? t
                : applyEvent(t, event),
            ),
          );
          if (event.type === 'done') break;
        }
      }
    } catch (err) {
      setTurns((cur) =>
        cur.map((t) =>
          t.id !== assistantTurn.id ? t : { ...t, error: String(err) },
        ),
      );
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  async function handoffToAgent(turn: ChatTurn) {
    if (!selectedProject) return;
    const userTurn = turns.find((t) => t.id === turn.id - 1 && t.role === 'user');
    const prompt = userTurn?.text || '';
    if (!prompt) return;
    setHandingOff(turn.id);
    try {
      const title = prompt.split('\n')[0].slice(0, 80);
      const description = `${prompt}\n\n---\nHermes routing: ${turn.routing?.intent ?? 'unknown'} → ${turn.routing?.model ?? 'unknown'}\n\nAssistant draft:\n${turn.text}`;
      const res = await fetch('/api/tasks', {
        method: 'POST',
        headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: selectedProject.id,
          title,
          description,
        }),
      });
      if (!res.ok) {
        let detail = `${res.status}`;
        try {
          const j = (await res.json()) as { detail?: unknown };
          if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
        } catch { /* ignore */ }
        throw new Error(detail);
      }
      setTurns((cur) =>
        cur.map((t) =>
          t.id !== turn.id
            ? t
            : { ...t, text: t.text + `\n\n[handed off to agent — see Kanban board]` },
        ),
      );
    } catch (e) {
      setTurns((cur) =>
        cur.map((t) =>
          t.id !== turn.id ? t : { ...t, error: `Handoff failed: ${(e as Error).message}` },
        ),
      );
    } finally {
      setHandingOff(null);
    }
  }

  return (
    <div className="flex flex-col h-screen bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center gap-4">
        <h1 className="text-xl font-semibold">Hermes</h1>
        <span className="text-xs text-muted-foreground">
          LLM-routing chat with project grounding
        </span>
        <div className="ml-auto flex items-center gap-3 text-sm">
          <label className="text-muted-foreground">Project</label>
          <select
            value={selectedSlug ?? ''}
            onChange={(e) => setSelectedSlug(e.target.value || null)}
            className="rounded-md border border-border bg-card px-3 py-1"
            disabled={streaming}
          >
            <option value="">(none)</option>
            {projects.map((p) => (
              <option key={p.id} value={p.slug}>
                {p.name} ({p.slug})
              </option>
            ))}
          </select>
        </div>
      </header>

      <main
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-6 py-4 space-y-4"
      >
        {turns.length === 0 && (
          <div className="text-center text-muted-foreground mt-10">
            Ask Hermes anything about your project. It will pick the right LLM and ground the answer in your docs.
          </div>
        )}
        {turns.map((t) => (
          <TurnBubble
            key={t.id}
            turn={t}
            canHandoff={Boolean(selectedProject) && !streaming && t.role === 'assistant' && (t.routing?.intent === 'code' || t.routing?.intent === 'plan')}
            isHandingOff={handingOff === t.id}
            onHandoff={() => void handoffToAgent(t)}
          />
        ))}
      </main>

      <footer className="border-t border-border px-6 py-4">
        <div className="flex gap-3">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={2}
            placeholder="Ask about the code, write a function, plan a change…"
            className="flex-1 rounded-lg border border-border bg-card px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary resize-none"
            disabled={streaming}
          />
          {streaming ? (
            <button
              type="button"
              onClick={stop}
              className="px-4 py-2 rounded-lg border border-border text-sm font-medium"
            >
              Stop
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void send()}
              disabled={sendDisabled}
              className="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
            >
              Send
            </button>
          )}
        </div>
      </footer>
    </div>
  );
}

function applyEvent(turn: ChatTurn, event: ChatEvent): ChatTurn {
  switch (event.type) {
    case 'routing':
      return { ...turn, routing: { intent: event.intent, model: event.model } };
    case 'citations':
      return { ...turn, citations: event.value };
    case 'token':
      return { ...turn, text: turn.text + event.value };
    case 'error':
      return { ...turn, error: event.value };
    case 'done':
      return turn;
  }
}

function TurnBubble({
  turn,
  canHandoff,
  isHandingOff,
  onHandoff,
}: {
  turn: ChatTurn;
  canHandoff: boolean;
  isHandingOff: boolean;
  onHandoff: () => void;
}) {
  const isUser = turn.role === 'user';
  return (
    <div className={isUser ? 'flex justify-end' : 'flex justify-start'}>
      <div
        className={
          'max-w-[80%] rounded-2xl px-4 py-3 shadow-sm ' +
          (isUser ? 'bg-primary text-primary-foreground' : 'bg-card border border-border')
        }
      >
        {turn.routing && !isUser && (
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
            {turn.routing.intent} → {turn.routing.model}
          </div>
        )}
        <div className="whitespace-pre-wrap font-mono text-sm leading-relaxed">{turn.text || (isUser ? '' : '…')}</div>
        {turn.error && (
          <div className="mt-2 rounded bg-destructive/10 border border-destructive/40 text-destructive text-xs px-2 py-1">
            {turn.error}
          </div>
        )}
        {turn.citations && turn.citations.length > 0 && (
          <div className="mt-3 border-t border-border pt-2 space-y-1">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Citations ({turn.citations.length})
            </div>
            {turn.citations.map((c, i) => (
              <div key={i} className="text-xs">
                <span className="font-mono">{c.file_path}:{c.line_start}</span>{' '}
                <span className="text-muted-foreground">— {c.heading || '(no heading)'}</span>
              </div>
            ))}
          </div>
        )}
        {canHandoff && (
          <div className="mt-3 border-t border-border pt-2 flex">
            <button
              type="button"
              onClick={onHandoff}
              disabled={isHandingOff}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-accent disabled:opacity-50"
              title="Create a Kanban task and let an agent execute this"
            >
              <ArrowRightCircle className="h-3.5 w-3.5" />
              {isHandingOff ? 'Handing off…' : 'Hand off to agent'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
