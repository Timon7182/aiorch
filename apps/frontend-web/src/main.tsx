import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './index.css';
import './shared/i18n';
import { initWebAPI } from './lib/api-adapter';
import { initializeGitHubListeners } from './stores/github';
import { initUsageWebSocketBridge } from './stores/usage-store';
import { initInsightsListeners } from './stores/insights-store';

// Initialize web API adapter (replaces window.API)
initWebAPI();

// Initialize global GitHub event listeners (PR review progress/complete/error)
// Must be called after initWebAPI() so window.API is available
initializeGitHubListeners();

// Global insights chat listeners: streaming chunks keep flowing into the store
// even when the Insights view is unmounted, and websocket reconnects trigger a
// session resync (prevents the "stuck on thinking" hang).
initInsightsListeners();

// Forward live `task:usage` WebSocket events into the usage store so per-task
// pills and the dashboard total update without polling.
initUsageWebSocketBridge();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
