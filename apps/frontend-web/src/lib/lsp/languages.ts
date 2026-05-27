/**
 * LSP-enabled languages.
 *
 * Maps a Monaco language id (as produced by EditorPage's `detectLanguage`) to
 * the backend WebSocket route segment for `/ws/lsp/{segment}`. Only languages
 * listed here get a language client; everything else renders in the plain
 * `@monaco-editor/react` editor with no behavior change.
 */

export interface LspLanguage {
  /** Monaco language id (e.g. 'python', 'typescript'). */
  monacoLanguage: string;
  /** Route segment: `/ws/lsp/<route>` — must match the backend allowlist. */
  route: string;
}

export const LSP_LANGUAGES: Record<string, LspLanguage> = {
  python: { monacoLanguage: 'python', route: 'python' },
  typescript: { monacoLanguage: 'typescript', route: 'typescript' },
  javascript: { monacoLanguage: 'javascript', route: 'javascript' },
};

/** True when the given Monaco language id has a backing language server. */
export function isLspLanguage(language?: string): language is string {
  return !!language && language in LSP_LANGUAGES;
}

/** The `/ws/lsp/{route}` segment for a language, or undefined if unsupported. */
export function lspRouteFor(language?: string): string | undefined {
  return language ? LSP_LANGUAGES[language]?.route : undefined;
}
