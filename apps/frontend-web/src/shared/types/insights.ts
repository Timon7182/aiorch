/**
 * Insights types
 */

import type { TaskMetadata } from './task';

// ============================================
// Insights Chat Types
// ============================================

import type { ThinkingLevel } from './settings';

// Supported LLM providers
export type InsightsProvider = 'claude' | 'codex' | 'gemini' | 'ollama'
  | 'lmstudio' | 'localai' | 'vllm' | 'jan' | 'openai_compat';

// Code-search backend used to ground the assistant's answers.
//   'auto'  → CodeGraph when the project is indexed, else plain file tools
//   'cgc'   → force CodeGraph MCP tools (only takes effect for Claude)
//   'files' → raw Read/Grep/Glob only
export type CodeSearchBackend = 'auto' | 'cgc' | 'graphify' | 'files';

// Freshness of the project's generated documentation relative to current code.
export interface DocsStatus {
  hasDocs: boolean;        // a docs/ tree exists in the run dir
  headSha: string | null;  // current short HEAD of the run dir
  docsSha: string | null;  // head_sha recorded at last docs generation
  fresh: boolean;          // headSha === docsSha (both known)
}

// Which code-search backends are usable for the branch/repo the chat will
// run against. Returned by the code-search-availability endpoint.
export interface CodeSearchAvailability {
  cgc: boolean;       // CodeGraph indexed + enabled + CLI present for that dir
  graphify: boolean;  // graphify-out/graph.json present for that dir
  docs?: DocsStatus;  // documentation freshness for that dir
}

// Model configuration for insights sessions
export interface InsightsModelConfig {
  provider: InsightsProvider;    // LLM provider (default: 'claude')
  profileId: string;             // 'complex' | 'balanced' | 'quick' | 'custom'
  model: string;                 // Model ID (e.g. 'opus', 'llama3:8b', 'gpt-4o')
  thinkingLevel?: ThinkingLevel; // Only applicable for Claude
  codeSearch?: CodeSearchBackend; // Code navigation backend (default: 'auto')
  dbProfileId?: string;          // Connect the chat to a registered DB (read-only); undefined = none
  logsEnabled?: boolean;         // Give the chat the read-only logs MCP server
  uiCheckEnabled?: boolean;      // Give the chat a headless browser (Playwright MCP) for UI checks
}

// A saved database connection profile (from the Databases extension)
export interface DatabaseProfileSummary {
  id: string;
  name: string;
  kind: string;       // 'postgres' | 'mysql' | 'sqlite'
  env?: string;
  database?: string;
  host?: string;
  projectIds?: string[]; // per-project scoping; empty/absent = global
}

// Provider info returned from detection endpoint
export interface InsightsProviderInfo {
  provider: InsightsProvider;
  available: boolean;
  displayName: string;
  icon: string;
  authMethod: string | null;
  models: { id: string; label: string }[];
}

export type InsightsChatRole = 'user' | 'assistant';

// An attachment sent with a chat message. Images are forwarded to vision-capable
// models (written to disk + read by the agent); text/code files have their
// contents inlined into the prompt. `data` is always base64 (no data-URL prefix)
// for a uniform transport contract — the backend decodes both kinds.
export interface ChatAttachment {
  id: string;
  kind: 'image' | 'text';
  filename: string;
  mimeType: string;
  size: number;        // bytes (decoded size)
  data: string;        // base64-encoded contents (images and text alike)
  thumbnail?: string;  // base64 data URL, images only — for the chip preview
}

// Tool usage record for showing what tools the AI used
export interface InsightsToolUsage {
  name: string;
  input?: string;        // Arguments the tool was called with (e.g. the SQL query)
  result?: string;       // Short summary of the tool's output (e.g. returned rows)
  isError?: boolean;     // True when the tool call returned an error
  timestamp: Date;
}

export interface InsightsChatMessage {
  id: string;
  role: InsightsChatRole;
  content: string;
  timestamp: Date;
  // For assistant messages that suggest task creation
  suggestedTask?: {
    title: string;
    description: string;
    metadata?: TaskMetadata;
  };
  // Tools used during this response (assistant messages only)
  toolsUsed?: InsightsToolUsage[];
  // Provider info (for showing badges on non-Claude messages)
  provider?: InsightsProvider;
  providerModel?: string;
  // Files/images attached to this message (user messages only)
  attachments?: ChatAttachment[];
}

export interface InsightsSession {
  id: string;
  projectId: string;
  title?: string; // Auto-generated from first message or user-set
  messages: InsightsChatMessage[];
  modelConfig?: InsightsModelConfig; // Per-session model configuration
  createdAt: Date;
  updatedAt: Date;
}

// Summary of a session for the history list (without full messages)
export interface InsightsSessionSummary {
  id: string;
  projectId: string;
  title: string;
  messageCount: number;
  modelConfig?: InsightsModelConfig; // For displaying model indicator in sidebar
  createdAt: Date;
  updatedAt: Date;
}

export interface InsightsChatStatus {
  phase: 'idle' | 'thinking' | 'streaming' | 'complete' | 'error';
  message?: string;
  error?: string;
}

export interface InsightsStreamMetrics {
  inputTokens?: number;
  outputTokens: number;
  tokensPerSecond: number;
  elapsedSeconds: number;
  estimated: boolean;       // true = char-based estimate, false = exact (e.g. Ollama)
}

export interface InsightsStreamChunk {
  type: 'text' | 'thinking' | 'task_suggestion' | 'tool_start' | 'tool_input' | 'tool_end' | 'done' | 'error';
  content?: string;
  suggestedTask?: {
    title: string;
    description: string;
    metadata?: TaskMetadata;
  };
  tool?: {
    name: string;
    input?: string;  // Brief description of what's being searched/read (full args on tool_input)
  };
  result?: string;   // Tool output summary (on tool_end)
  isError?: boolean; // Tool returned an error (on tool_end)
  error?: string;
  metrics?: InsightsStreamMetrics;
}
