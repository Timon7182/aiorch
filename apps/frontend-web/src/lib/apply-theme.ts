import type { AppSettings, ColorTheme } from '../shared/types';

/** Theme mode as stored in settings: 'light' | 'dark' | 'system'. */
export type ThemeMode = AppSettings['theme'];

// localStorage keys — MUST stay in sync with the pre-paint inline script in
// index.html so first paint reproduces exactly what React later computes.
export const THEME_STORAGE_KEY = 'magestic-theme';
export const COLOR_THEME_STORAGE_KEY = 'magestic-color-theme';
export const DEFAULT_COLOR_THEME: ColorTheme = 'ocean';

/** Resolve whether dark mode should be active for a given theme mode. */
export function isDarkMode(theme: ThemeMode): boolean {
  if (theme === 'dark') return true;
  if (theme === 'light') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

/**
 * Persist the theme preference to localStorage without touching the DOM.
 * Call this the moment settings arrive/change so the inline script in
 * index.html can apply the correct theme synchronously on the next load.
 */
export function persistTheme(
  theme: ThemeMode,
  colorTheme: ColorTheme = DEFAULT_COLOR_THEME
): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
    localStorage.setItem(COLOR_THEME_STORAGE_KEY, colorTheme);
  } catch {
    // localStorage may be unavailable (private mode, etc.)
  }
}

/**
 * Single source of truth for applying the theme to <html>: toggles `.dark`,
 * sets `data-theme`, and persists both values to localStorage. Every runtime
 * writer of the theme class must go through here so the DOM and the cached
 * first-paint hints never drift apart.
 */
export function applyTheme(
  theme: ThemeMode,
  colorTheme: ColorTheme = DEFAULT_COLOR_THEME
): void {
  const root = document.documentElement;
  root.setAttribute('data-theme', colorTheme);
  root.classList.toggle('dark', isDarkMode(theme));
  persistTheme(theme, colorTheme);
}
