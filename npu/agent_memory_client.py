"""Client for sending Agent lifecycle events to the local bridge."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AgentMemoryClient:
    """Small dependency-free client for the bridge's localhost event endpoint."""

    def __init__(self, endpoint: str = "http://127.0.0.1:8003/agent-events", timeout: float = 2.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def emit(self, event: str, **fields: object) -> None:
        payload = {"event": event, **fields}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(f"agent event endpoint returned HTTP {response.status}")
        except (HTTPError, URLError) as error:
            raise RuntimeError(f"unable to report Agent event to {self.endpoint}: {error}") from error

    def upsert_agent(self, agent_id: str, **fields: object) -> None:
        self.emit("upsert", agent_id=agent_id, **fields)

    def set_state(self, agent_id: str, *, state: str, stage: str | None = None) -> None:
        fields: dict[str, object] = {"agent_id": agent_id, "state": state}
        if stage is not None:
            fields["stage"] = stage
        self.emit("state", **fields)

    def set_memory(self, agent_id: str, **fields: object) -> None:
        self.emit("memory", agent_id=agent_id, **fields)

    def set_kv_totals(self, *, hits: int | float, misses: int | float) -> None:
        self.emit("kv_totals", hits=hits, misses=misses)

    def record_recompute(self, tokens: int | float) -> None:
        self.emit("recompute", tokens=tokens)

    def record_transfer(
        self,
        operation: str,
        *,
        bytes_count: int | float,
        duration_seconds: int | float,
        source_tier: str,
        target_tier: str,
    ) -> None:
        self.emit(
            "transfer",
            operation=operation,
            bytes_count=bytes_count,
            duration_seconds=duration_seconds,
            source_tier=source_tier,
            target_tier=target_tier,
        )

    def remove_agent(self, agent_id: str) -> None:
        self.emit("remove", agent_id=agent_id)


__all__ = ["AgentMemoryClient"]
