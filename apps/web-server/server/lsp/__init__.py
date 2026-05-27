"""LSP (Language Server Protocol) bridge package.

Spawns language-server subprocesses and bridges JSON-RPC between a browser
Monaco editor (via monaco-languageclient + vscode-ws-jsonrpc) and the server's
stdio. Mirrors the structure of the ``server/pty`` package, but uses
cross-platform ``asyncio`` subprocesses (LSP speaks plain stdio JSON-RPC, no PTY).
"""
