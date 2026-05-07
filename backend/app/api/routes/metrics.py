from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return "\n".join(
        [
            "# HELP biche_api_up API process health",
            "# TYPE biche_api_up gauge",
            "biche_api_up 1",
            "# HELP biche_provider_error_total Provider errors observed by workers",
            "# TYPE biche_provider_error_total counter",
            "biche_provider_error_total 0",
        ]
    )
