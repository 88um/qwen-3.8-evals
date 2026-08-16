"""Tenant/token resolution.

tenants.json (written by the harness before serve starts) is the source of
truth for who may call what. Loaded once at startup into an in-memory map.
"""

import json
from pathlib import Path


class Principal:
    __slots__ = ("role", "tenant_id")

    def __init__(self, role: str, tenant_id: str | None = None):
        self.role = role          # 'tenant' | 'admin'
        self.tenant_id = tenant_id


class TenantRegistry:
    def __init__(self, tenants: list[dict], admin_token: str):
        self._token_to_tenant: dict[str, str] = {}
        for t in tenants:
            tid = t["id"]
            tok = t["token"]
            if tok in self._token_to_tenant:
                raise ValueError(f"duplicate token {tok!r}")
            self._token_to_tenant[tok] = tid
        self._admin_token = admin_token

    def resolve(self, token: str | None) -> Principal | None:
        """Map a bearer token to a principal, or None if unknown."""
        if not token:
            return None
        if token == self._admin_token:
            return Principal("admin")
        tid = self._token_to_tenant.get(token)
        if tid is None:
            return None
        return Principal("tenant", tid)


def load(path: Path) -> TenantRegistry:
    data = json.loads(path.read_text())
    tenants = data.get("tenants", [])
    admin_token = data.get("admin_token", "")
    return TenantRegistry(tenants, admin_token)
