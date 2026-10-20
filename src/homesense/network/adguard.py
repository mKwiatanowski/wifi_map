from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from homesense.core.models import DnsQueryRecord

log = logging.getLogger(__name__)

__all__ = ["AdGuardClient", "AdGuardError", "parse_querylog"]


class AdGuardError(Exception):
    """AdGuard Home is unreachable or answered with an error (non-2xx, bad payload)."""


def _domain_rule(domain: str) -> str:
    return f"||{domain.strip().lower().rstrip('.')}^"


def _client_rule(ip: str) -> str:
    return f"||*^$client={ip.strip()}"


class AdGuardClient:
    """Thin async client for the AdGuard Home control API."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = httpx.BasicAuth(username, password) if username else None
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=10)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, f"{self._base_url}{path}", auth=self._auth, **kwargs)
        except httpx.HTTPError as exc:
            raise AdGuardError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise AdGuardError(f"{method} {path} returned HTTP {response.status_code}")
        return response

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise AdGuardError(f"GET {path} returned invalid JSON") from exc

    async def status(self) -> dict:
        return await self._get_json("/control/status")

    async def querylog(self, older_than: str | None = None, limit: int = 500) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if older_than:
            params["older_than"] = older_than
        return await self._get_json("/control/querylog", params=params)

    async def blocked_rules(self) -> list[str]:
        payload = await self._get_json("/control/filtering/status")
        return list(payload.get("user_rules") or [])

    async def _set_rules(self, rules: list[str]) -> None:
        await self._request("POST", "/control/filtering/set_rules", json={"rules": rules})

    async def _add_rule(self, rule: str) -> None:
        rules = await self.blocked_rules()
        if rule not in rules:
            await self._set_rules([*rules, rule])

    async def _remove_rule(self, rule: str) -> None:
        rules = await self.blocked_rules()
        if rule in rules:
            await self._set_rules([r for r in rules if r != rule])

    async def block_domain(self, domain: str) -> None:
        await self._add_rule(_domain_rule(domain))

    async def unblock_domain(self, domain: str) -> None:
        await self._remove_rule(_domain_rule(domain))

    async def block_client(self, ip: str) -> None:
        await self._add_rule(_client_rule(ip))

    async def unblock_client(self, ip: str) -> None:
        await self._remove_rule(_client_rule(ip))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()


def _parse_item(item: dict) -> DnsQueryRecord:
    ts = datetime.fromisoformat(item["time"])
    if ts.tzinfo is None:
        raise ValueError("querylog time without offset")
    reason = item.get("reason") or None
    return DnsQueryRecord(
        ts=ts.astimezone(UTC),
        client_ip=item["client"],
        domain=item["question"]["name"].strip().lower().rstrip("."),
        blocked=bool(reason and reason.startswith("Filtered")),
        reason=reason,
    )


def parse_querylog(payload: dict) -> list[DnsQueryRecord]:
    """Map a ``/control/querylog`` payload to records in payload order; malformed items are skipped."""
    records: list[DnsQueryRecord] = []
    for item in payload.get("data") or []:
        try:
            records.append(_parse_item(item))
        except (KeyError, TypeError, ValueError, AttributeError):
            log.debug("skipping malformed querylog item: %r", item)
    return records
