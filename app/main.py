from fastapi import FastAPI
from app.api.health import router as health_router

app = FastAPI(title="Rush API")
app.include_router(health_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Hello World"}
