# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Any, Callable, Optional, Tuple, Union

from budget_tools import __version__
from budget_tools.rate_limit import RateLimiter

BASE_URL = "https://api.ynab.com/v1"
USER_AGENT = f"ynab-agent-skill/{__version__}"


@dataclass
class ApiError(RuntimeError):
    message: str
    status: int | None = None
    error_name: str | None = None
    detail: str | None = None

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.error_name:
            parts.append(self.error_name)
        if self.detail:
            parts.append(self.detail)
        return ": ".join(parts)


Transport = Callable[[str, str, dict[str, str], Optional[bytes], float],
                     Union[Tuple[int, bytes], Tuple[int, bytes, dict[str, str]]]]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Authenticated API requests must never leave the fixed YNAB origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ApiError("The YNAB API returned an unexpected redirect", status=code)


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_RejectRedirects())
    with opener.open(request, timeout=timeout) as response:
        return int(response.status), response.read(), dict(response.headers.items())


class YnabClient:
    """Fixed-origin HTTP client; authorization belongs to the workflow modules.

    Tests can inject a transport to exercise request serialization without a
    network connection. Error details supplied by the API can contain private data.
    """

    def __init__(
        self,
        access_token: str,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("An access token is required")
        self._access_token = access_token
        self._timeout = timeout
        self._transport = transport or _default_transport
        self._rate_limiter = rate_limiter
        self._write_guard = None

    @contextmanager
    def write_guard(self, revalidate: Callable[[], None], before_send: Callable[[], None],
                    rate_limited: Callable[[], None]):
        """Workflow hooks keep delayed writes fresh and checkpoints accurate.

        A client is used by one command at a time. Revalidation may make GETs;
        hooks are never applied to those reads.
        """
        if self._write_guard is not None:
            raise RuntimeError("Nested write guards are not supported")
        self._write_guard = (revalidate, before_send, rate_limited)
        try:
            yield
        finally:
            self._write_guard = None

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int | None] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Wait/retry confirmed 429 rejections, never retry uncertain writes.

        A timeout or unreadable response does not prove that a write failed. The
        workflow caller must preserve uncertainty and reconcile before another write.
        """
        if not path.startswith("/") or "://" in path or ".." in path:
            raise ValueError("API paths must be absolute paths on the fixed YNAB API host")

        query = ""
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            query = "?" + urllib.parse.urlencode(clean) if clean else ""

        url = f"{BASE_URL}{path}{query}"
        payload = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        guard = self._write_guard if method not in {"GET", "HEAD"} else None
        revalidate = False
        while True:
            if revalidate and guard:
                guard[0]()
                revalidate = False
            waited = self._rate_limiter.acquire() if self._rate_limiter else False
            if waited and guard:
                # The reserved slot remains charged, conservatively. Validation
                # needs a fresh read, which may itself have to wait for capacity.
                revalidate = True
                continue
            if guard:
                guard[1]()
            try:
                result = self._transport(method, url, headers, payload, self._timeout)
                status, raw = result[:2]
                response_headers = result[2] if len(result) == 3 else {}
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read()
                response_headers = dict(exc.headers.items()) if exc.headers else {}
                exc.close()
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                raise ApiError("The YNAB API request did not complete") from exc
            except OSError as exc:
                raise ApiError("The YNAB API could not be reached") from exc
            if status == 429 and guard:
                guard[2]()
            if self._rate_limiter:
                self._rate_limiter.observe(status, response_headers)
            if status == 429 and self._rate_limiter:
                revalidate = True
                continue
            break

        if status < 200 or status >= 300:
            raise self._api_error(status, raw)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("The YNAB API returned an invalid JSON response", status=status) from exc
        if not isinstance(parsed, dict):
            raise ApiError("The YNAB API returned an unexpected response", status=status)
        return parsed

    @staticmethod
    def _api_error(status: int, raw: bytes) -> ApiError:
        name = None
        detail = None
        try:
            parsed = json.loads(raw.decode("utf-8"))
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            if isinstance(error, dict):
                name = str(error.get("name")) if error.get("name") else None
                detail = str(error.get("detail")) if error.get("detail") else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return ApiError("YNAB API request failed", status=status, error_name=name, detail=detail)

    def get_user(self) -> dict[str, Any]:
        return self.request("GET", "/user")["data"]["user"]

    def get_plans(self) -> list[dict[str, Any]]:
        return self.request("GET", "/plans")["data"]["plans"]

    def get_accounts(self, plan_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/plans/{_safe_id(plan_id)}/accounts")["data"]["accounts"]

    def get_plan_settings(self, plan_id: str) -> dict[str, Any]:
        return self.request("GET", f"/plans/{_safe_id(plan_id)}/settings")["data"]["settings"]

    def get_categories(self, plan_id: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"/plans/{_safe_id(plan_id)}/categories")["data"]
        return data["category_groups"]

    def get_payees(self, plan_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/plans/{_safe_id(plan_id)}/payees")["data"]["payees"]

    def get_months(self, plan_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/plans/{_safe_id(plan_id)}/months")["data"]["months"]

    def get_month(self, plan_id: str, month: str) -> dict[str, Any]:
        return self.request(
            "GET", f"/plans/{_safe_id(plan_id)}/months/{_safe_id(month)}"
        )["data"]["month"]

    def update_month_category(self, plan_id: str, month: str, category_id: str,
                              budgeted: int) -> dict[str, Any]:
        return self.request(
            "PATCH", f"/plans/{_safe_id(plan_id)}/months/{_safe_id(month)}/categories/{_safe_id(category_id)}",
            body={"category": {"budgeted": budgeted}},
        )

    def get_transactions(
        self,
        plan_id: str,
        *,
        since_date: str,
        until_date: str | None = None,
        transaction_type: str | None = None,
    ) -> list[dict[str, Any]]:
        response = self.request(
            "GET",
            f"/plans/{_safe_id(plan_id)}/transactions",
            params={
                "since_date": since_date,
                "until_date": until_date,
                "type": transaction_type,
            },
        )
        return response["data"]["transactions"]

    def get_transactions_by_account(
        self,
        plan_id: str,
        account_id: str,
        *,
        since_date: str,
        until_date: str | None = None,
        transaction_type: str | None = None,
    ) -> list[dict[str, Any]]:
        response = self.request(
            "GET",
            f"/plans/{_safe_id(plan_id)}/accounts/{_safe_id(account_id)}/transactions",
            params={
                "since_date": since_date,
                "until_date": until_date,
                "type": transaction_type,
            },
        )
        return response["data"]["transactions"]

    def get_transaction(self, plan_id: str, transaction_id: str) -> dict[str, Any]:
        response = self.request(
            "GET",
            f"/plans/{_safe_id(plan_id)}/transactions/{_safe_id(transaction_id)}",
        )
        return response["data"]["transaction"]

    def create_transaction(self, plan_id: str, transaction: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/plans/{_safe_id(plan_id)}/transactions",
            body={"transaction": transaction},
        )

    def update_transaction(
        self,
        plan_id: str,
        transaction_id: str,
        transaction: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "PUT",
            f"/plans/{_safe_id(plan_id)}/transactions/{_safe_id(transaction_id)}",
            body={"transaction": transaction},
        )

    def delete_transaction(self, plan_id: str, transaction_id: str) -> dict[str, Any]:
        """Transport only; supported callers must use guarded draft execution."""
        return self.request(
            "DELETE",
            f"/plans/{_safe_id(plan_id)}/transactions/{_safe_id(transaction_id)}",
        )


def _safe_id(value: str) -> str:
    # IDs are opaque path segments. Posted recurring entries can retain an
    # underscore/date suffix; preserve it even if the transaction date changes.
    # Delimiters, percent escapes, dot segments and non-ASCII remain forbidden.
    if (not isinstance(value, str) or not value
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in value)):
        raise ValueError("An invalid resource id was provided")
    return value
