import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import close_pool
from router import orchestrator, router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hazardmind")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect the orchestrator to the Band room on startup.
    try:
        await orchestrator.connect()
    except Exception:  # noqa: BLE001 - API still serves reads if Band is down
        logger.exception("Orchestrator failed to connect on startup")
    yield
    await close_pool()


app = FastAPI(
    title="HazardMind AI Backend",
    description="Multi-agent disaster response system",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "hazardmind-backend"}
