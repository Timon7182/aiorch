/**
 * LSP-enabled Monaco editor.
 *
 * Used ONLY for languages with a backing language server (see
 * `lib/lsp/languages.ts`). Wraps `@typefox/monaco-editor-react`, which sets up
 * the `@codingame/monaco-vscode-api` services + editor + language client. The
 * client connects to the backend bridge at `/ws/lsp/{route}?root=...&token=...`.
 *
 * This module pulls the heavy `@codingame` stack, so it is imported lazily
 * (`React.lazy`) from EditorPage and only loads when an LSP file is opened.
 * Plain files keep using `@monaco-editor/react` with zero behavior change.
 */

import { useMemo } from 'react';
import * as vscode from 'vscode';
import { LogLevel } from '@codingame/monaco-vscode-api';
import { MonacoEditorReactComp } from '@typefox/monaco-editor-react';
import type { MonacoVscodeApiConfig } from 'monaco-languageclient/vscodeApiWrapper';
import type { EditorAppConfig } from 'monaco-languageclient/editorApp';
import type { LanguageClientConfig } from 'monaco-languageclient/lcwrapper';
import { configureDefaultWorkerFactory } from 'monaco-languageclient/workerFactory';
import { CloseAction, ErrorAction } from 'vscode-languageclient/browser.js';

import { getAuthenticatedWsUrl } from '@/lib/auth';
import { lspRouteFor } from '@/lib/lsp/languages';

// Register the languages we support so Monaco has grammars/config for them and
// the language client's documentSelector matches. Side-effect imports.
import '@codingame/monaco-vscode-python-default-extension';
import '@codingame/monaco-vscode-typescript-basics-default-extension';

interface LspEditorProps {
  /** Absolute path of the file being edited (used as the model URI). */
  path: string;
  /** Monaco language id (e.g. 'python', 'typescript'). */
  language: string;
  /** Absolute path of the workspace/project root (the LSP workspace folder). */
  projectPath: string;
  /** File content. */
  value: string;
  /** Called with the new content on every edit. */
  onChange: (value: string) => void;
}

// The @codingame VSCode API is a global singleton initialized once; this config
// is honored on the first editor mount and ignored (with a log) thereafter.
const vscodeApiConfig: MonacoVscodeApiConfig = {
  $type: 'classic',
  viewsConfig: { $type: 'EditorService' },
  logLevel: LogLevel.Warning,
  monacoWorkerFactory: configureDefaultWorkerFactory,
};

export default function LspEditor({ path, language, projectPath, value, onChange }: LspEditorProps) {
  const route = lspRouteFor(language) ?? language;

  // Build configs once per (file, language, root). EditorPage gives this
  // component a `key={path}` so a new file remounts a fresh editor + client.
  const editorAppConfig = useMemo<EditorAppConfig>(
    () => ({
      codeResources: {
        modified: {
          text: value,
          uri: vscode.Uri.file(path).toString(),
        },
      },
      editorOptions: {
        theme: 'vs-dark',
        minimap: { enabled: true },
        fontSize: 14,
        lineNumbers: 'on',
        wordWrap: 'on',
        automaticLayout: true,
        scrollBeyondLastLine: false,
      },
    }),
    // `value` intentionally excluded: it's the initial content only; live edits
    // flow through onTextChanged. Remount (key=path) handles file switches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [path],
  );

  const languageClientConfig = useMemo<LanguageClientConfig>(
    () => ({
      languageId: language,
      connection: {
        options: {
          $type: 'WebSocketUrl',
          url: getAuthenticatedWsUrl(
            `/ws/lsp/${route}?root=${encodeURIComponent(projectPath)}`,
          ),
        },
      },
      clientOptions: {
        documentSelector: [language],
        workspaceFolder: {
          index: 0,
          name: 'workspace',
          uri: vscode.Uri.file(projectPath),
        },
        // Our backend runs one server per connection and closes the socket when
        // the server exits — don't fight it with reconnect loops.
        errorHandler: {
          error: () => ({ action: ErrorAction.Continue }),
          closed: () => ({ action: CloseAction.DoNotRestart }),
        },
      },
    }),
    [language, route, projectPath],
  );

  return (
    <MonacoEditorReactComp
      style={{ height: '100%', width: '100%' }}
      vscodeApiConfig={vscodeApiConfig}
      editorAppConfig={editorAppConfig}
      languageClientConfig={languageClientConfig}
      onTextChanged={(tc) => {
        if (tc.modified !== undefined) onChange(tc.modified);
      }}
      onError={(err) => console.error('[LspEditor]', err)}
    />
  );
}
