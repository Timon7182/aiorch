# Preview deploy engine

One-click "Deploy preview" for finished AI tasks: build a task's worktree → run an
isolated Docker stack on a host → return a live URL/IP. Plus two long-lived **static
lanes** (A = main/pre-prod, B = test) and a **Promote** step. Previews auto-terminate
(concurrency cap + TTL).

## Pieces
- `preview-runner.sh` — the generic host engine (deploy/teardown/promote/extend/list/reap/doctor).
  Installed **once per host**, allowlisted in a MagesticAI `ServerProfile` as
  `deploys["preview"]`. Adding a new project needs **no** new host script.
- `preview.env.example` — host secrets + golden DB names + ports + domain. Copy to
  `/etc/magestic-preview/preview.env` (chmod 600).
- `deploy.config.example.json` — per-project config consumed by the runner (the cts shape).
- Python: `services/preview_deploy_service.py` (orchestrator), `services/deploy_config.py`
  (config loader + lane mapping), `services/ssh_service.py::run_script` (allowlisted invoker).
- Routes: `POST/GET/DELETE /tasks/{id}/deploy-preview`,
  `POST /tasks/{id}/deploy-preview/extend` (+Nh), `POST /tasks/{id}/promote`.

## One-time host setup (target = 192.168.88.55, NOT cargo-preprod which is 92% full)
```bash
# 1. install the runner
sudo install -m 0755 preview-runner.sh /usr/local/bin/preview-runner
# 2. host config
sudo install -d -m 0700 /etc/magestic-preview
sudo cp preview.env.example /etc/magestic-preview/preview.env   # then edit + chmod 600
# 3. tools
sudo apt-get install -y jq postgresql-client     # docker + nginx already present
# 4. build the golden + static lane DBs from cts pre-prod (one-time; needs the cts DB role)
SRC_SSH=cargo-preprod SRC_DB=cts SRC_PG_CONTAINER=cts-db-postgres-1 SRC_PG_USER=<cts-role> \
  ./setup-golden-dbs.sh                       # optionally: --sanitize sanitize.sql
# 5. sanity
preview-runner doctor
```

## Auto-teardown (TTL + extend)
Each preview is stamped with an `expires_at` at deploy time (= now + TTL, default
**2h**, from `--ttl-hours` or `PREVIEW_TTL_HOURS`). `services/preview_reaper.py` runs
in the web-server and, every `PREVIEW_REAP_INTERVAL_MIN` (default **5**), tears down any
preview whose `expires_at` has passed, on every server with a `deploys.preview` runner.
`PREVIEW_TTL_HOURS` (default 2) is now only the **fallback** lifetime for legacy previews
that predate `expires_at`. The UI shows a live countdown and, near expiry, a "need more
time?" banner that calls `extend` (+1h); each extend bumps `expires_at`. Set
`PREVIEW_REAPER_ENABLED=0` to disable. Previews are also torn down on Stop, Promote,
and task delete.
Then register a `ServerProfile` for the host in MagesticAI with
`deploys = { "preview": "/usr/local/bin/preview-runner" }`.

## DB model
Each preview clones the lane's golden DB via `CREATE DATABASE preview_<task> TEMPLATE
cts_golden_<lane>` (fast, one shared Postgres, no per-preview container). Teardown drops it.
Promote points the static lane at its persistent DB (`cts_static_<lane>`).
