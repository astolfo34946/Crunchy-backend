# Crunchyroll checker API (Render)

Self-contained backend for [Render](https://render.com) or any Python host.

## Deploy on Render (backend-only GitHub repo)

1. Create a **new GitHub repository** and upload **only the files in this folder** to the repo root (`bot.py`, `server.py`, `requirements.txt`, `runtime.txt`, `render.yaml`).
2. In Render: **New → Blueprint** → connect the repo → apply (or **New → Web Service** with the settings below).

**Manual Web Service settings**

| Setting        | Value |
|----------------|--------|
| Runtime        | Python |
| Build command  | `pip install -r requirements.txt` |
| Start command  | `uvicorn server:app --host 0.0.0.0 --port $PORT` |
| Root directory | *(empty if this repo is only these files)* |

3. After deploy, copy your service URL (e.g. `https://crunchyroll-checker-api.onrender.com`).

4. **CORS:** In Render → your service → **Environment**, set:

   `CORS_ORIGINS` = `https://your-frontend-domain.com`

   Comma-separate multiple origins if needed. No trailing slash. **`http://localhost:5173`** (and a few other local dev URLs) are **always allowed** in code—no need to list them unless you want to be explicit.

5. **Redeploy** after env or code changes (Dashboard → Manual Deploy).

6. **Frontend:** Build the React app with `VITE_API_URL` set to that same API URL (see `web/.env.example` in the main project).

## Optional environment variables

| Variable         | Purpose |
|------------------|---------|
| `CRUNCHYROLL_AUTH` | `Basic …` OAuth client header if the bundled client is rotated |
| `CORS_ORIGINS`     | Comma-separated production origins (local Vite URLs are always merged in) |

## Monorepo

If this project lives inside a larger repo under `backend/`, use the `render.yaml` at the **repository root** (one level up), which sets `rootDir: backend`.

## Health checks

- `GET /api/health` → `{"ok":true}`
- `GET /api/ping` → `{"pong":true}`
