from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("")
async def get_health() -> dict[str, str]:
    return {"status": "ok"}
