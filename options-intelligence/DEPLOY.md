# Deploy Bull50 Options Intelligence (short guide)

Paper trading and research only. Nothing in this system places real orders.

## 1. Get the code

```bash
git clone https://github.com/satyajit8080/Dhan.git
cd Dhan
git checkout claude/tender-dijkstra-wzbqrg
cd options-intelligence
```

## 2. Configure

```bash
cp .env.example .env
mkdir -p secrets
echo "YOUR_DHAN_ACCESS_TOKEN" > secrets/dhan.token
chmod 600 secrets/dhan.token .env
```

Edit `.env`:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | A strong password |
| `DATABASE_URL` | `postgresql://doi:<same password>@db:5432/doi` (inside Docker the host is `db`, not `localhost`) |
| `DHAN_CLIENT_ID` | Your Dhan client id |
| `AI_ENABLED` | Keep `false` until you've run the Grok check in step 6 |
| `XAI_API_KEY` | Your xAI key (optional) |

## 3. Start

```bash
docker compose up -d --build
docker compose logs -f api    # wait for "Application startup complete"
```

Database migrations run automatically at start-up.

## 4. Create your login key

```bash
docker compose exec api python -m doi.core.auth create-key --email you@example.com --role admin
```

The key is shown only once, so copy it somewhere safe. The roles are `viewer`, `approver` and `admin`.

## 5. Open the dashboard

Go to `http://127.0.0.1:8000/dashboard` and paste your key.

Check the status endpoint, using the key from step 4:

```bash
curl -H "Authorization: Bearer YOUR_KEY" http://127.0.0.1:8000/api/v1/system/status
```

## 6. Before using it live

1. Test the Dhan connection:

   ```bash
   docker compose exec api python /scripts/smoke_live.py
   ```

2. Enable an index by setting `live_enabled: true` in `backend/doi/intelligence/strategy_configs/<index>.yaml`, then rebuild with `docker compose up -d --build`. The configs are baked into the image.

3. Optionally test Grok, which spends at most ₹20:

   ```bash
   cd backend && pip install ".[dev]" && XAI_API_KEY=... python ../scripts/grok_live_check.py
   ```

   Run this from a checkout, not inside the container.

   If it passes, set `AI_ENABLED=true` and run `docker compose restart api ai-worker`.

## Without Docker

Requires Python 3.12+ and PostgreSQL 16.

```bash
cd backend
pip install .
export DATABASE_URL=postgresql://doi:PASS@localhost:5432/doi DHAN_CLIENT_ID=... DHAN_TOKEN_FILE=../secrets/dhan.token
python -m doi.db.migrate
python -m doi.core.auth create-key --email you@example.com --role admin
uvicorn doi.app.main:create_app --factory --host 127.0.0.1 --port 8000   # API
python -m doi.ai.worker                                                  # AI worker, in a second terminal
```

## Everyday operations

| Task | How |
|---|---|
| Emergency stop | Dashboard, or `POST /api/v1/admin/kill-switch {"on": true}` (admin key) |
| Daily report | `GET /api/v1/reports/daily` |
| Grok spend | The dashboard's Budget tab. The hard cap is ₹3,000 a month |
| Backup | `docker compose exec db pg_dump -U doi doi > backup.sql` |
| Update | `git pull && docker compose up -d --build` |

The detailed guide is `docs/phase7-deployment.md`, and the current verification status is `docs/phase8-validation.md`.

## Setup Wizard (recommended)
Open `/dashboard` → **⚙ Settings & Keys** → **Setup Wizard** and follow the steps: Dhan client ID → Dhan token (Test) → indices → Grok key (Test, free) → enable AI → optional live feed → **↻ Apply & restart**. Choices are saved in `secrets/setup.json`; values in `.env` override them (shown as SET IN .env).

## Keys from the dashboard
Open `/dashboard` → **⚙ Settings & Keys** (admin key required, HTTPS only):
- Dhan client ID, Dhan access token, Grok API key: write-only, saved as 0600 files in `SECRETS_DIR` (mount it as a volume). The Dhan token and an existing Grok key apply live; the Dhan client ID or a first Grok key need `docker compose restart api ai-worker`.
- Dashboard API keys: create (shown once), list, revoke. The last admin key cannot be revoked.
- Every change is in the audit log without the value. Values set in `.env` override saved files.
- One-time on the server: `mkdir -p secrets && sudo chown 10001:10001 secrets && chmod 700 secrets` (the container runs as uid 10001).
