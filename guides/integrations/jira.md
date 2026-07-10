# JIRA / Atlassian Integration

> Status: **configuration only** — the connection settings can be entered and
> stored today via **Admin → Integrations**. The sync job that imports issues
> and pushes status back is not implemented yet. This document describes the
> setup the UI captures and how to wire the sync when it's built.

JIRA Software referenced: **v7.4.2 (#74004-sha1:586975d)** — note that Server/
Data Center 7.x uses **basic auth / personal tokens** against the
`/rest/api/2/` REST API, whereas Atlassian **Cloud** uses an **email + API
token** pair against `/rest/api/3/`. The admin form fields map to both; pick
the auth style that matches your deployment.

## 1. Configure the connection (admin UI)

1. Open the app as a global admin (`role == "admin"`).
2. Go to the **Admin** screen in the sidebar → **Integrations** tab.
3. Fill in:
   - **Base URL** — your JIRA site, e.g. `https://your-domain.atlassian.net`
     (Cloud) or `https://jira.your-company.com` (Server/DC 7.4.2).
   - **Account email** — the Atlassian account the token belongs to (Cloud).
     For Server/DC basic auth this is the username.
   - **API token** — Atlassian Cloud API token, or a Personal Access Token /
     password for Server/DC.
   - **Project key** — e.g. `ENG`, to scope sync to one JIRA project.
   - **JQL** (optional) — extra filter, e.g.
     `status != Done ORDER BY created DESC`.
4. Toggle **Enabled** and **Save**.

### Creating an API token

- **Cloud:** Atlassian → *Account settings → Security → Create and manage API
  tokens → Create API token*.
- **Server / Data Center 7.4.2:** *Profile → Personal Access Tokens* (if
  enabled by your admin), otherwise use basic auth with your username/password
  over HTTPS.

## 2. Where the config is stored

The admin form persists to the `integration_settings` table
(`key = "jira"`), with the secret in `config_json`. The API token is **masked**
(`********`) whenever the config is read back, and re-saving the form without
re-typing the token keeps the stored value.

Backend:
- Model: `apps/web-server/server/database/models.py` → `IntegrationSetting`
- Routes: `apps/web-server/server/routes/admin.py`
  - `GET /api/admin/integrations/jira`
  - `PUT /api/admin/integrations/jira`

## 3. Implementing the sync (TODO)

Create `apps/web-server/server/integrations/jira/` (or a backend module under
`apps/backend/integrations/jira/`) that:

1. Reads `IntegrationSetting(key="jira")`; bail out early if `enabled` is false.
2. Builds an authenticated client:
   - Cloud: HTTP basic with `(email, api_token)` against `/rest/api/3/search`.
   - Server/DC 7.4.2: basic auth or `Bearer <PAT>` against `/rest/api/2/search`.
3. Runs the configured JQL (default `project = <project_key>`), paging through
   results.
4. Maps each JIRA issue → a MagesticAI task (`tasks`/`projects.json` spec dir),
   keying on the issue key to upsert idempotently.
5. Pushes local status transitions back via
   `POST /rest/api/{2,3}/issue/{key}/transitions`.

Trigger options:
- Manual button on the Integrations tab (`POST /api/admin/integrations/jira/sync`).
- Scheduled poll (reuse the `preview_reaper`-style background task pattern in
  `server/main.py` lifespan).

Keep all JIRA HTTP calls server-side; never expose the API token to the
frontend (the GET endpoint already masks it).
