"""
Crunchyroll checker API for production (Render, etc.).

CORS: set CORS_ORIGINS to a comma-separated list of frontend URLs, e.g.:
  https://my-app.vercel.app,http://localhost:5173

Render start command (also in render.yaml):
  uvicorn server:app --host 0.0.0.0 --port $PORT

Local:
  python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bot import (
    MAX_ACCOUNTS_PER_RUN,
    CheckResult,
    RunSummary,
    build_summary,
    check_result_to_dict,
    iter_checks_sequential,
    run_checks_collecting,
)

app = FastAPI(title="Crunchyroll Checker API")

# Built-in Firebase Hosting / default app URLs (no trailing slash). Add more via CORS_ORIGINS on Render.
_CORS_BUILTIN = [
    "https://crunchyrool-checker.web.app",
    "https://crunchyrool-cheker.firebaseapp.com",
]

# Local Vite/React dev (always merged).
_CORS_DEV_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
]


def _cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    from_env = [o.strip() for o in raw.split(",") if o.strip()] if raw else []
    seen: set[str] = set()
    out: list[str] = []
    for o in from_env + _CORS_BUILTIN + _CORS_DEV_ORIGINS:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CheckRequest(BaseModel):
    combos: str = Field(..., description="One email:password per line")
    threads: int = Field(1, ge=1, le=2, description="Ignored; API uses sequential checks.")
    delay: float = Field(0.0, ge=0.0, le=120.0)
    proxies: Optional[str] = Field(
        None,
        description="Optional: one proxy URL per line. Empty = direct connection.",
    )


class CheckResponse(BaseModel):
    summary: dict
    results: List[dict]


def _summary_dict(s: RunSummary) -> dict:
    return {
        "total": s.total,
        "valid": s.valid,
        "invalid": s.invalid,
        "errors": s.errors,
        "bad_format": s.bad_format,
        "seconds": round(s.seconds, 2),
        "valid_lines": s.valid_lines,
    }


def _parse_lines(combos: str) -> List[str]:
    return [ln.strip() for ln in combos.splitlines() if ln.strip()]


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/ping")
def ping():
    return {"pong": True}


@app.post("/api/check/stream")
def check_stream(body: CheckRequest):
    """NDJSON stream: one JSON object per line — progress events + final done."""
    lines = _parse_lines(body.combos)
    if not lines:
        raise HTTPException(status_code=400, detail="No combos provided")
    if len(lines) > MAX_ACCOUNTS_PER_RUN:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_ACCOUNTS_PER_RUN} accounts per run",
        )

    proxy_list: Optional[List[str]] = None
    if body.proxies and body.proxies.strip():
        proxy_list = [ln.strip() for ln in body.proxies.splitlines() if ln.strip()]

    total = len(lines)

    def ndjson_gen():
        start = time.perf_counter()
        results: List[CheckResult] = []
        for result in iter_checks_sequential(lines, proxy_list, body.delay):
            results.append(result)
            line = json.dumps(
                {
                    "type": "progress",
                    "current": len(results),
                    "total": total,
                    "result": check_result_to_dict(result),
                },
                ensure_ascii=False,
            )
            yield line + "\n"
        elapsed = time.perf_counter() - start
        summary = build_summary(results, elapsed)
        yield json.dumps({"type": "done", "summary": _summary_dict(summary)}, ensure_ascii=False) + "\n"

    return StreamingResponse(ndjson_gen(), media_type="application/x-ndjson")


@app.post("/api/check", response_model=CheckResponse)
async def check_accounts(body: CheckRequest):
    lines = _parse_lines(body.combos)
    if not lines:
        raise HTTPException(status_code=400, detail="No combos provided")
    if len(lines) > MAX_ACCOUNTS_PER_RUN:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_ACCOUNTS_PER_RUN} accounts per run",
        )

    proxy_list: Optional[List[str]] = None
    if body.proxies and body.proxies.strip():
        proxy_list = [ln.strip() for ln in body.proxies.splitlines() if ln.strip()]

    def job():
        return run_checks_collecting(
            lines,
            max_workers=1,
            proxy_urls=proxy_list,
            extra_delay=body.delay,
        )

    try:
        summary, results = await asyncio.to_thread(job)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return CheckResponse(
        summary=_summary_dict(summary),
        results=[check_result_to_dict(r) for r in results],
    )
