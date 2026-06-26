# MagesticAI — Где код и как деплоить

> ⚠️ Содержит внутренние инфраструктурные детали (IP сервера, путь к SSH-ключу,
> расположение секретов). Не публиковать наружу.

## 📍 Где находится код

**Git-репозиторий (единственный):**
- `https://github.com/Timon7182/aiorch.git` — remote `origin`, ветка **`main`**.
  Все коммиты и деплои идут сюда. Форк `dataseeek/MagesticAI` — read-only, для пушей не используется.

**Три места, где живёт код:**

| Где | Путь | Назначение |
|-----|------|-----------|
| 🖥️ Локально (ПК) | `C:\Users\User\Desktop\magestic` | рабочая копия, ветка `main` |
| 🗄️ Прод-сервер (хост) | `saya@192.168.88.55:/home/saya/magestic` | git-дерево = build-контекст Docker |
| 🐳 Внутри контейнера | `/home/projects/MagesticAI/` | работающий код (фронт пресобран в `apps/web-server/static/`) |

**Структура (`apps/`):**
- `apps/backend/` — логика агентов (планнер/кодер/QA), `phase_config.py`, `core/client.py`
- `apps/web-server/` — FastAPI бэкенд веб-UI (REST + WebSocket), сервисы чата (`insights_providers/`)
- `apps/frontend-web/` — React/TS фронтенд (Vite)

**Доступ к серверу:**
```bash
ssh -i ~/.ssh/digitalocean_impact saya@192.168.88.55

# шелл внутрь контейнера:
ssh -i ~/.ssh/digitalocean_impact -t saya@192.168.88.55 'docker exec -u magesticai -it magesticai bash'

# логи:
ssh -i ~/.ssh/digitalocean_impact saya@192.168.88.55 'docker logs magesticai --tail=50'
```

**Порт:** веб-сервер на `3101`. Health-проба: `curl http://127.0.0.1:3101/api/health`.

---

## 🚀 Как деплоить

> ⚠️ Прод-дерево содержит незакоммиченную работу — **никогда `git reset --hard`**.
> GitHub Actions деплой сломан (пустые SSH-секреты) → только ручной SSH.

Есть два пути.

### Вариант A — хирургический патч (рекомендуемый, безопаснее всего)

Накатывает только ваши изменения поверх того, что уже на проде.

```bash
# --- локально: собрать патч из своих правок ---
git diff > /tmp/my.patch
scp -i ~/.ssh/digitalocean_impact /tmp/my.patch saya@192.168.88.55:/tmp/my.patch

# --- на сервере ---
cd /home/saya/magestic
git apply --check --ignore-whitespace /tmp/my.patch   # проверка применимости
git apply --ignore-whitespace /tmp/my.patch           # накат

set -a; source /home/saya/.aiorch-secrets; set +a     # секреты (GEMINI / OAuth / git-токены)

docker compose -p magesticai-server -f .ops/compose.server.yml build > /tmp/build.log 2>&1; echo "BUILD_EXIT=$?"
docker compose -p magesticai-server -f .ops/compose.server.yml up -d --force-recreate

# health-poll:
for i in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:3101/api/health && { echo "HEALTHY"; break; }; sleep 3; done
```

### Вариант B — полное дерево (когда baseline на проде устарел / много файлов)

```bash
# --- локально ---
git archive --format=tar.gz -o /tmp/d.tgz HEAD
scp -i ~/.ssh/digitalocean_impact /tmp/d.tgz saya@192.168.88.55:/tmp/d.tgz

# --- на сервере ---
cd /home/saya/magestic && tar xzf /tmp/d.tgz            # extract поверх дерева (секреты/.env не трогает)
set -a; source /home/saya/.aiorch-secrets; set +a
docker compose -p magesticai-server -f .ops/compose.server.yml build > /tmp/build.log 2>&1; echo "BUILD_EXIT=$?"
docker compose -p magesticai-server -f .ops/compose.server.yml up -d --force-recreate
```

> На Windows (`core.autocrlf=true`) `git archive` может переписать `*.sh` в CRLF →
> крэш-луп entrypoint. Защита — закоммиченный `.gitattributes` с `*.sh text eol=lf`.
> Быстрый фикс в контексте: `find . -name '*.sh' -print0 | xargs -0 sed -i 's/\r$//'`.

---

## ⛔ Критичные правила (стоили целых сессий отладки)

1. **Всегда** `-p magesticai-server -f .ops/compose.server.yml`. Без `-p` compose
   возьмёт имя проекта из папки `.ops` → проект `ops`, который дерётся за
   `container_name: magesticai` (конфликты имён, «No such container»).
2. **Источай секреты** перед `up`, иначе `GEMINI_API_KEY` пустой → Hermes падает
   («GEMINI_API_KEY not configured»). GEMINI идёт через `env_file`
   (`/home/saya/.aiorch.env`), OAuth/git-токены — через `${VAR:-}` из `.aiorch-secrets`.
3. **Фиксируй реальный exit сборки** (`build > log 2>&1; echo $?`). Пайп с `tail`
   возвращает код `tail` (всегда 0) → падение фронтенда маскируется, и `up`
   пересоздаёт контейнер на **старом** образе (прод «здоров» на старом коде).
4. **Сборка ~4–5 мин** (любая правка кода бьёт pip-слой `COPY .`). Фронтенд
   `tsc && vite build` идёт **внутри** образа — TS-ошибки валят деплой.
5. **Не запускай два деплоя параллельно** — параллельный build бакает stale-код.

---

## ✅ Проверка после деплоя

```bash
# бэкенд — sentinel-строка в живом контейнере:
docker exec magesticai grep -c "<ваша-строка>" /home/projects/MagesticAI/apps/.../file.py

# фронтенд — в собранном бандле (доказывает, что vite реально пересобрался):
docker exec magesticai sh -lc 'grep -rl "<ваша-строка>" /home/projects/MagesticAI/apps/web-server/static/assets/'

# GEMINI на месте:
docker inspect magesticai --format '{{range .Config.Env}}{{println .}}{{end}}' | grep ^GEMINI_API_KEY=
```

---

## 🔑 Авторизация Claude (если чат отвечает «Not logged in» / 401)

Токен живёт в персистентном volume `magesticai-claude-config`
(`/home/magesticai/.claude/.credentials.json`), обновляется `claude_token_service`.
Две частые причины 401:
- статический env `CLAUDE_CODE_OAUTH_TOKEN` **затеняет** авто-обновляемые volume-creds
  (фикс: закомментировать в `.aiorch-secrets` + `.aiorch.env`, force-recreate);
- **stale `:ro` seed-inode** с протухшим refresh-токеном (фикс: `docker cp` свежих
  creds с хоста в volume + `--force-recreate` для перепривязки seed-mount).

Быстрое восстановление creds (без рестарта, сервис читает файл вживую):
```bash
chmod 644 /home/saya/.claude/.credentials.json
cp /home/saya/.claude/.credentials.json /tmp/cc.json
docker cp /tmp/cc.json magesticai:/home/magesticai/.claude/.credentials.json
docker exec -u root magesticai sh -lc 'chown magesticai:magesticai /home/magesticai/.claude/.credentials.json && chmod 600 /home/magesticai/.claude/.credentials.json'
```

---

## 📂 Где что на сервере (хост)

- `/home/saya/magestic/` — git-дерево + build-контекст
- `/home/saya/magestic/.ops/{compose.server.yml}` — compose-конфиг деплоя
- `/home/saya/.aiorch-secrets` — `export VAR=...` (GEMINI, GITLAB_TOKEN, OAuth) — источается перед `up`
- `/home/saya/.aiorch.env` — `KEY=VAL` форма (из `.aiorch-secrets` без `export`), `env_file` для compose
- `/home/saya/magestic-data/` — хост-сторона bind-mount `.magestic-ai/` (data.db, projects.json, .token)
- `/home/saya/projects/` — bind-mount пользовательских проектов (контейнер видит их в `/home/magesticai/projects/`)
