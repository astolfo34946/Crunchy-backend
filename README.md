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

4. **CORS:** The API allows **`https://crunchyrool-checker.web.app`** and **`https://crunchyrool-cheker.firebaseapp.com`** by default (Firebase Hosting). For **any other** frontend URL, set in Render → **Environment**:

   `CORS_ORIGINS` = `https://your-other-domain.com`

   Comma-separate multiple origins. **No trailing slash.** Local dev (`http://localhost:5173`, etc.) is always merged in.

   If the browser says *“blocked by CORS policy”* / *“No Access-Control-Allow-Origin”*, the **exact** page origin (scheme + host + port) is not in the allowed list—add it to `CORS_ORIGINS` and redeploy.

5. **Redeploy** after env or code changes (Dashboard → Manual Deploy).

6. **Frontend:** Build the React app with `VITE_API_URL` set to that same API URL (see `web/.env.example` in the main project).

## Optional environment variables

| Variable         | Purpose |
|------------------|---------|
| `CRUNCHYROLL_AUTH` | `Basic …` OAuth client header if the bundled client is rotated |
| `CORS_ORIGINS`     | Extra comma-separated origins (Firebase defaults + localhost are already merged in `server.py`) |

## Monorepo

If this project lives inside a larger repo under `backend/`, use the `render.yaml` at the **repository root** (one level up), which sets `rootDir: backend`.

## Health checks

- `GET /api/health` → `{"ok":true}`
- `GET /api/ping` → `{"pong":true}`
