from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from fastapi import FastAPI
from app.core.db import engine
from app.api.health import router as health_router
from app.api.drops import router as drops_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="Rush API", lifespan=lifespan)
app.include_router(health_router)
app.include_router(drops_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "Hello World"}
