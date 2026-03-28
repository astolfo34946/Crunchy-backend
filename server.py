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
import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from bot import RunSummary, check_result_to_dict, run_checks_collecting

app = FastAPI(title="Crunchyroll Checker API")


def _cors_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CheckRequest(BaseModel):
    combos: str = Field(..., description="One email:password per line")
    threads: int = Field(3, ge=1, le=10)
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


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/check", response_model=CheckResponse)
async def check_accounts(body: CheckRequest):
    lines = [ln.strip() for ln in body.combos.splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="No combos provided")

    proxy_list: Optional[List[str]] = None
    if body.proxies and body.proxies.strip():
        proxy_list = [ln.strip() for ln in body.proxies.splitlines() if ln.strip()]

    def job():
        return run_checks_collecting(
            lines,
            max_workers=body.threads,
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
