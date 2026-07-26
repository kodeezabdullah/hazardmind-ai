import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db import close_pool, ping as db_ping
from router import orchestrator, router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hazardmind")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Compile the LangGraph pipeline once on startup.
    try:
        await orchestrator.connect()
    except Exception:  # noqa: BLE001 - don't block the API from serving reads
        logger.exception("Orchestrator failed to initialize on startup")

    # Background cleanup: every CLEANUP_INTERVAL_HOURS (default 12), clear
    # stuck events so they stop holding a concurrency slot. Best-effort; never
    # blocks startup.
    import asyncio as _asyncio

    from cleanup import cleanup_loop

    cleanup_task = _asyncio.create_task(cleanup_loop())

    yield

    cleanup_task.cancel()
    await close_pool()


app = FastAPI(
    title="HazardMind AI Backend",
    description="Multi-agent disaster response system",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS origins are env-driven for production. Set ALLOWED_ORIGINS to a
# comma-separated list of front-end URLs (e.g. "https://hazardmind.vercel.app").
# Defaults to "*" only when unset, which is convenient for local development but
# should always be locked down in production.
_origins_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
_allowed_origins = (
    ["*"] if _origins_env in ("", "*")
    else [o.strip() for o in _origins_env.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
async def health():
    db_ok = await db_ping()
    graph_ok = getattr(orchestrator, "_graph", None) is not None
    return {
        "status": "ok",
        "service": "hazardmind-backend",
        "pipeline": "ready" if graph_ok else "not_initialized",
        "db": "connected" if db_ok else "disconnected",
        "version": app.version,
    }
