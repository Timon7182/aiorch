/**
 * WebSocket manager for real-time features
 * Handles terminal, logs, and progress streaming
 */

import { getAuthenticatedWsUrl } from './auth';
import { refreshAccessToken } from './api-client';

type MessageHandler = (data: unknown) => void;
type ConnectionHandler = () => void;

interface WebSocketConnection {
  ws: WebSocket;
  reconnectTimeout?: ReturnType<typeof setTimeout>;
}

class WebSocketManager {
  private connections: Map<string, WebSocketConnection> = new Map();
  // Handlers live at the manager level, keyed by endpoint, so they survive
  // reconnects. They used to live on the connection object: a reconnect built
  // a fresh connection with an empty handler set, silently killing all event
  // delivery (insights chunks, task events) until a full page reload.
  private handlers: Map<string, Set<MessageHandler>> = new Map();
  // Reconnect attempts also live at the manager level — the per-connection
  // counter was reset by every reconnect, so backoff never actually grew.
  private reconnectAttempts: Map<string, number> = new Map();
  private onConnectHandlers: Map<string, Set<ConnectionHandler>> = new Map();
  private onDisconnectHandlers: Map<string, Set<ConnectionHandler>> = new Map();
  private reconnectDelay = 1000;
  private maxReconnectDelay = 30000;

  constructor() {
    // Self-heal after laptop sleep / backgrounded tabs / network blips: the
    // server drops silent sockets (uvicorn ping timeout ~20s), so re-check the
    // moment we're plausibly back online or visible again.
    if (typeof window !== 'undefined') {
      window.addEventListener('online', () => this.reconnectStale());
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') this.reconnectStale();
      });
    }
  }

  /** Reconnect any endpoint that still has subscribers but no live socket. */
  private reconnectStale(): void {
    for (const [endpoint, handlers] of this.handlers) {
      if (handlers.size === 0) continue;
      const connection = this.connections.get(endpoint);
      const state = connection?.ws.readyState;
      if (state !== WebSocket.OPEN && state !== WebSocket.CONNECTING) {
        console.log(`[WebSocket] Reviving stale connection: ${endpoint}`);
        this.connections.delete(endpoint);
        this.connect(endpoint);
      }
    }
  }

  /**
   * Connect to a WebSocket endpoint
   */
  connect(endpoint: string): WebSocket {
    const existing = this.connections.get(endpoint);
    if (
      existing &&
      (existing.ws.readyState === WebSocket.OPEN ||
        existing.ws.readyState === WebSocket.CONNECTING)
    ) {
      return existing.ws;
    }
    if (existing?.reconnectTimeout) {
      clearTimeout(existing.reconnectTimeout);
    }

    const url = getAuthenticatedWsUrl(endpoint);
    const ws = new WebSocket(url);

    const connection: WebSocketConnection = { ws };

    ws.onopen = () => {
      console.log(`[WebSocket] Connected: ${endpoint}`);
      this.reconnectAttempts.set(endpoint, 0);
      this.onConnectHandlers.get(endpoint)?.forEach((h) => h());
    };

    ws.onmessage = (event) => {
      const handlers = this.handlers.get(endpoint);
      if (!handlers || handlers.size === 0) return;
      try {
        const data = JSON.parse(event.data);
        handlers.forEach((handler) => handler(data));
      } catch {
        // Handle non-JSON messages (e.g., terminal raw output)
        handlers.forEach((handler) => handler(event.data));
      }
    };

    ws.onclose = (event) => {
      console.log(`[WebSocket] Disconnected: ${endpoint}`, event.code, event.reason);
      this.onDisconnectHandlers.get(endpoint)?.forEach((h) => h());

      // Reconnect for non-normal closures while anyone is still subscribed.
      // No attempt cap: this is the app's event channel — giving up means the
      // UI silently stops updating. Backoff is capped at maxReconnectDelay.
      if (event.code === 1000) return;
      if ((this.handlers.get(endpoint)?.size ?? 0) === 0) return;

      const attempts = (this.reconnectAttempts.get(endpoint) ?? 0) + 1;
      this.reconnectAttempts.set(endpoint, attempts);
      const delay = Math.min(
        this.reconnectDelay * Math.pow(2, attempts - 1),
        this.maxReconnectDelay,
      );
      console.log(`[WebSocket] Reconnecting in ${delay}ms (attempt ${attempts})`);
      connection.reconnectTimeout = setTimeout(async () => {
        // 4001 = server rejected our auth: the access token baked into the
        // connect URL has expired. REST refreshes transparently on 401; do the
        // same here before retrying (getAuthenticatedWsUrl re-reads the token).
        if (event.code === 4001) {
          try {
            await refreshAccessToken();
          } catch {
            // Fall through — retry with whatever token we have.
          }
        }
        this.connections.delete(endpoint);
        this.connect(endpoint);
      }, delay);
    };

    ws.onerror = (error) => {
      console.error(`[WebSocket] Error: ${endpoint}`, error);
    };

    this.connections.set(endpoint, connection);
    return ws;
  }

  /**
   * Subscribe to messages on an endpoint. The subscription survives
   * reconnects; unsubscribe is keyed by endpoint, not by socket instance.
   */
  subscribe(endpoint: string, handler: MessageHandler): () => void {
    if (!this.handlers.has(endpoint)) {
      this.handlers.set(endpoint, new Set());
    }
    this.handlers.get(endpoint)!.add(handler);
    this.connect(endpoint);

    return () => {
      const handlers = this.handlers.get(endpoint);
      handlers?.delete(handler);
      // Close connection if no more handlers
      if (handlers && handlers.size === 0) {
        this.disconnect(endpoint);
      }
    };
  }

  /**
   * Send data through WebSocket
   */
  send(endpoint: string, data: unknown): boolean {
    const connection = this.connections.get(endpoint);
    if (!connection || connection.ws.readyState !== WebSocket.OPEN) {
      console.warn(`[WebSocket] Cannot send, not connected: ${endpoint}`);
      return false;
    }

    const message = typeof data === 'string' ? data : JSON.stringify(data);
    connection.ws.send(message);
    return true;
  }

  /**
   * Disconnect from endpoint
   */
  disconnect(endpoint: string): void {
    const connection = this.connections.get(endpoint);
    if (connection) {
      if (connection.reconnectTimeout) {
        clearTimeout(connection.reconnectTimeout);
      }
      connection.ws.close(1000, 'Client disconnect');
      this.connections.delete(endpoint);
    }
    this.reconnectAttempts.delete(endpoint);
  }

  /**
   * Register connection handler
   */
  onConnect(endpoint: string, handler: ConnectionHandler): () => void {
    if (!this.onConnectHandlers.has(endpoint)) {
      this.onConnectHandlers.set(endpoint, new Set());
    }
    this.onConnectHandlers.get(endpoint)!.add(handler);
    return () => this.onConnectHandlers.get(endpoint)?.delete(handler);
  }

  /**
   * Register disconnect handler
   */
  onDisconnect(endpoint: string, handler: ConnectionHandler): () => void {
    if (!this.onDisconnectHandlers.has(endpoint)) {
      this.onDisconnectHandlers.set(endpoint, new Set());
    }
    this.onDisconnectHandlers.get(endpoint)!.add(handler);
    return () => this.onDisconnectHandlers.get(endpoint)?.delete(handler);
  }

  /**
   * Get connection state
   */
  isConnected(endpoint: string): boolean {
    const connection = this.connections.get(endpoint);
    return connection?.ws.readyState === WebSocket.OPEN;
  }

  /**
   * Disconnect all
   */
  disconnectAll(): void {
    for (const endpoint of this.connections.keys()) {
      this.disconnect(endpoint);
    }
  }
}

// Singleton instance
export const wsManager = new WebSocketManager();

// Convenience functions for specific WebSocket types
export const terminalWs = {
  connect: (terminalId: string) => wsManager.connect(`/ws/terminal/${terminalId}`),
  subscribe: (terminalId: string, handler: MessageHandler) =>
    wsManager.subscribe(`/ws/terminal/${terminalId}`, handler),
  send: (terminalId: string, data: string) =>
    wsManager.send(`/ws/terminal/${terminalId}`, data),
  disconnect: (terminalId: string) =>
    wsManager.disconnect(`/ws/terminal/${terminalId}`),
};

export const taskLogsWs = {
  subscribe: (taskId: string, handler: MessageHandler) =>
    wsManager.subscribe(`/ws/tasks/${taskId}/logs`, handler),
  disconnect: (taskId: string) =>
    wsManager.disconnect(`/ws/tasks/${taskId}/logs`),
};

export const taskProgressWs = {
  subscribe: (taskId: string, handler: MessageHandler) =>
    wsManager.subscribe(`/ws/tasks/${taskId}/progress`, handler),
  disconnect: (taskId: string) =>
    wsManager.disconnect(`/ws/tasks/${taskId}/progress`),
};
