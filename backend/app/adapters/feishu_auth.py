from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings


class FeishuAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantAccessToken:
    token: str
    expires_at: datetime

    def is_valid(self) -> bool:
        return datetime.now(timezone.utc) < self.expires_at


class FeishuAuthClient:
    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._app_id = app_id or settings.feishu_app_id
        self._app_secret = app_secret or settings.feishu_app_secret
        self._base_url = (base_url or settings.feishu_base_url).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._cached_tenant_token: TenantAccessToken | None = None
        self._lock = asyncio.Lock()

    async def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._cached_tenant_token and self._cached_tenant_token.is_valid():
            return self._cached_tenant_token.token

        async with self._lock:
            if not force_refresh and self._cached_tenant_token and self._cached_tenant_token.is_valid():
                return self._cached_tenant_token.token
            self._cached_tenant_token = await self._fetch_tenant_access_token()
            return self._cached_tenant_token.token

    async def _fetch_tenant_access_token(self) -> TenantAccessToken:
        if not self._app_id or not self._app_secret:
            raise FeishuAuthError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")

        url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self._app_id, "app_secret": self._app_secret}

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            try:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise FeishuAuthError("failed to request Feishu tenant_access_token") from exc

        body: dict[str, Any] = response.json()
        if body.get("code") != 0:
            code = body.get("code", "UNKNOWN")
            message = body.get("msg") or body.get("message") or "Feishu auth failed"
            raise FeishuAuthError(f"Feishu auth failed: code={code}, message={message}")

        token = body.get("tenant_access_token")
        if not token:
            raise FeishuAuthError("Feishu auth response did not include tenant_access_token")

        expires_in = int(body.get("expire") or 7200)
        # Refresh early so long-running workers do not hit expiry mid-request.
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(expires_in - 300, 60))
        return TenantAccessToken(token=token, expires_at=expires_at)
