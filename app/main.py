"""Standalone runnable app for local testing of the Preflight module.

This is NOT meant to replace your existing Supervea FastAPI app — in
production, mount `preflight_router` and `preflight_lifespan` into your
existing app instead. This file exists so you can `uvicorn app.main:app`
and hit POST /api/v1/preflight directly while developing.

    uvicorn app.main:app --reload --port 8001
"""

import logging

from fastapi import FastAPI

from app.preflight.api.lifespan import preflight_lifespan
from app.preflight.api.router import router as preflight_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Supervea Preflight MVP (OpenRouter-first)", lifespan=preflight_lifespan)
app.include_router(preflight_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
