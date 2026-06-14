from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from router import router

app = FastAPI(
    title="HazardMind AI Backend",
    description="Multi-agent disaster response system",
    version="1.0.0"
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
