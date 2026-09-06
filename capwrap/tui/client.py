"""Talking to a running capwrap from the terminal console.

Deliberately over HTTP rather than by importing the daemon: the TUI is then the
same kind of client as the browser, works against an instance started by someone
else, and works over an SSH session to the box the agents are actually on --
which is the case it exists for.

Standard library only, and synchronous. The console redraws a handful of times a
second against a local socket; an async client would buy nothing and cost the
whole file being written twice.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ConsoleError(Exception):
    """The daemon refused, or is not there."""


class Client:
    """One capwrap instance's HTTP API."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 8420, timeout: float = 10.0
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base = f"http://{host}:{port}"

    # ------------------------------------------------------------------

    def _call(self, path: str, method: str = "GET", body: Any = None) -> Any:
        request = urllib.request.Request(
            f"{self.base}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                body = json.loads(detail)
                detail = body.get("error") or body.get("detail") or detail
            except json.JSONDecodeError:
                pass
            raise ConsoleError(f"{exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise ConsoleError(
                f"no capwrap answering on {self.base} ({exc.reason})"
            ) from None
        except TimeoutError:
            raise ConsoleError(f"{self.base} did not answer in time") from None

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    def instance(self) -> str:
        return (self._call("/api/instance") or {}).get("name", "")

    def overview(self) -> dict:
        return self._call("/api/overview")

    def screens(self, rows: int = 40) -> list[dict]:
        return (self._call(f"/api/screens?rows={rows}") or {}).get("screens", [])

    def screen(self, name: str, rows: int = 40) -> dict:
        return self._call(
            f"/api/containers/{urllib.parse.quote(name)}/screen?rows={rows}"
        )

    def caps(self, name: str) -> list[dict]:
        return self._call(f"/api/caps/{urllib.parse.quote(name)}")

    def boards(self) -> list[dict]:
        return (self._call("/api/boards?limit=50") or {}).get("boards", [])

    def audit(self, limit: int = 100, denied: bool = False) -> list[dict]:
        return self._call(f"/api/audit?limit={limit}&denied={str(denied).lower()}")

    def traced(self, limit: int = 200) -> dict:
        return self._call(f"/api/messages?limit={limit}")

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------

    def answer(
        self,
        approval_id: int,
        decision: str,
        reason: str = "",
        rights: list[str] | None = None,
    ) -> Any:
        return self._call(
            f"/api/approvals/{approval_id}",
            "POST",
            {"decision": decision, "reason": reason, "rights": rights},
        )

    def send(self, targets: list[str], message: str) -> Any:
        return self._call("/api/send", "POST", {"targets": targets, "message": message})

    def start(self, name: str) -> Any:
        return self._call(f"/api/containers/{urllib.parse.quote(name)}/start", "POST")

    def stop(self, name: str) -> Any:
        return self._call(f"/api/containers/{urllib.parse.quote(name)}/stop", "POST")

    def interrupt(self, name: str) -> Any:
        return self._call(
            f"/api/containers/{urllib.parse.quote(name)}/signal?sig=2", "POST"
        )

    def set_trace(self, enabled: bool) -> Any:
        return self._call("/api/trace", "POST", {"enabled": enabled})

    def write_input(self, name: str, data: str) -> Any:
        return self._call(
            f"/api/containers/{urllib.parse.quote(name)}/input",
            "POST",
            {"data": data},
        )
