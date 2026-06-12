from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="HazardMind AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "HazardMind AI API Running"}

@app.post("/analyze")
async def analyze(disaster_type: str, location: str):
    return {"status": "processing", "job_id": "placeholder"}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    return {"job_id": job_id, "status": "processing"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
