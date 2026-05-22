/**
 * Login page — email/password by default, with a fallback to API token entry
 * for service-account / legacy flows.
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { useAuthStore } from '../stores/auth-store';

type Mode = 'login' | 'register' | 'token';

export function LoginPage() {
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [token, setToken] = useState('');

  const { login, loginWithCredentials, register, isLoading, error } = useAuthStore();
  const navigate = useNavigate();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    let ok = false;
    if (mode === 'login') ok = await loginWithCredentials(email.trim(), password);
    else if (mode === 'register') ok = await register(email.trim(), name.trim(), password);
    else ok = await login(token.trim());
    if (ok) navigate('/');
  };

  const passwordOk = password.length >= 8;
  const submitDisabled =
    isLoading ||
    (mode === 'login' && (!email || !password)) ||
    (mode === 'register' && (!email || !name || !passwordOk)) ||
    (mode === 'token' && !token);

  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="w-full max-w-md p-8">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-foreground">MagesticAI</h1>
          <p className="text-muted-foreground mt-2">
            {mode === 'register'
              ? 'Create an account'
              : mode === 'token'
                ? 'Use a service token'
                : 'Sign in to continue'}
          </p>
        </div>

        <div className="flex gap-1 mb-6 rounded-lg bg-muted p-1 text-sm font-medium">
          {(['login', 'register', 'token'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              className={
                'flex-1 rounded-md py-2 transition-colors ' +
                (mode === m
                  ? 'bg-card text-foreground shadow'
                  : 'text-muted-foreground hover:text-foreground')
              }
            >
              {m === 'login' ? 'Sign in' : m === 'register' ? 'Register' : 'API token'}
            </button>
          ))}
        </div>

        <form onSubmit={submit} className="space-y-5">
          {mode !== 'token' && (
            <>
              <div>
                <label
                  htmlFor="email"
                  className="block text-sm font-medium text-foreground mb-2"
                >
                  Email
                </label>
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="w-full px-4 py-3 rounded-lg border border-border bg-card text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                  placeholder="you@example.com"
                  required
                />
              </div>

              {mode === 'register' && (
                <div>
                  <label
                    htmlFor="name"
                    className="block text-sm font-medium text-foreground mb-2"
                  >
                    Name
                  </label>
                  <input
                    id="name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    className="w-full px-4 py-3 rounded-lg border border-border bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                    placeholder="Your name"
                    required
                  />
                </div>
              )}

              <div>
                <label
                  htmlFor="password"
                  className="block text-sm font-medium text-foreground mb-2"
                >
                  Password
                </label>
                <input
                  id="password"
                  type="password"
                  autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-4 py-3 rounded-lg border border-border bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                  placeholder={mode === 'register' ? 'At least 8 characters' : '••••••••'}
                  required
                  minLength={mode === 'register' ? 8 : 1}
                />
                {mode === 'register' && password && !passwordOk && (
                  <p className="mt-2 text-xs text-destructive">
                    Password must be at least 8 characters.
                  </p>
                )}
              </div>
            </>
          )}

          {mode === 'token' && (
            <div>
              <label
                htmlFor="token"
                className="block text-sm font-medium text-foreground mb-2"
              >
                API token
              </label>
              <input
                id="token"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                className="w-full px-4 py-3 rounded-lg border border-border bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
                placeholder="Paste the server's APP_API_TOKEN"
                required
              />
              <p className="mt-2 text-xs text-muted-foreground">
                Use this only for service accounts or recovery. Personal accounts should sign in above.
              </p>
            </div>
          )}

          {error && (
            <div className="p-3 rounded-lg bg-destructive/10 border border-destructive/50 text-destructive text-sm">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={submitDisabled}
            className="w-full py-3 px-4 bg-primary text-primary-foreground rounded-lg font-medium hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {isLoading
              ? 'Working…'
              : mode === 'register'
                ? 'Create account'
                : mode === 'token'
                  ? 'Sign in with token'
                  : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
