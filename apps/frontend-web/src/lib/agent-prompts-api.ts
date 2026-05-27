/**
 * Agent Prompts API
 *
 * Thin typed client for per-project agent prompt overrides. Calls the
 * api-client helpers directly (rather than going through window.API) so the
 * central API interface doesn't need to grow for this self-contained feature.
 *
 * NOTE: prompt keys may contain '/' (e.g. "github/pr_reviewer.md"). They are
 * passed raw because the backend route uses a `{prompt_key:path}` converter
 * that matches slashes — do NOT encodeURIComponent the key.
 */

import { get, put, del } from './api-client';

export interface PromptCatalogEntry {
  key: string;
  category: string;
  displayName: string;
  sizeBytes: number;
  isOverridden?: boolean;
  updatedAt?: string | null;
}

export interface EffectivePrompt {
  key: string;
  category: string;
  displayName: string;
  default: string;
  override: string | null;
  isOverridden: boolean;
  content: string;
  updatedAt: string | null;
}

/** List the catalog annotated with this project's override status. */
export function getProjectPrompts(projectId: string) {
  return get<PromptCatalogEntry[]>(`/projects/${projectId}/prompts`);
}

/** Fetch the effective prompt (default + override + content) for one key. */
export function getProjectPrompt(projectId: string, key: string) {
  return get<EffectivePrompt>(`/projects/${projectId}/prompts/${key}`);
}

/** Save (create or update) a project's override for a prompt. */
export function saveProjectPrompt(projectId: string, key: string, content: string) {
  return put<EffectivePrompt>(`/projects/${projectId}/prompts/${key}`, { content });
}

/** Reset a prompt to its bundled default (delete the override). */
export function resetProjectPrompt(projectId: string, key: string) {
  return del<EffectivePrompt>(`/projects/${projectId}/prompts/${key}`);
}
