# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Private, process-shared rolling-window accounting for the YNAB API."""

from __future__ import annotations

import hashlib
import math
import re
import sys
import time
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping

from budget_tools.state import StateError, StateStore

WINDOW = 3600.0
LIMIT = 200
MARGIN = 1.0


def _number(value: object) -> bool:
    # Bounded comparisons reject NaN/infinity and enormous corrupt JSON integers
    # without converting the latter to a float (which could itself overflow).
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 253_402_300_799)


class RateLimiter:
    """Reserve before sending; retain counts even after crashes or lost responses.

    Only a token digest, timestamps and counts are saved, never request contents.
    Locks cover accounting, never sleep or network I/O. A server count can reveal
    other clients; unknown request ages are conservatively charged for a full hour.
    """

    def __init__(self, store: StateStore, token: str, *,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 notify: Callable[[str], None] | None = None):
        self.store = store
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.path = store.root / f"rate-limit-{digest}.json"
        self.scope = f"rate-limit:{digest}"
        self.clock = clock
        self.sleep = sleep
        self.notify = notify or (lambda message: print(message, file=sys.stderr, flush=True))

    def _load(self, now: float) -> dict:
        # Missing is normal on first use; unsafe/corrupt existing files fail closed.
        state = self.store.load_rate_limit(self.path)
        if state is None:
            state = {"version": 1, "requests": [], "blocked_until": 0, "last_seen": now}
        if (type(state.get("version")) is not int or state["version"] != 1
                or not isinstance(state.get("requests"), list)
                or len(state["requests"]) > LIMIT
                or not all(_number(t) for t in state["requests"])
                or not _number(state.get("blocked_until"))
                or not _number(state.get("last_seen"))):
            raise StateError("Invalid rate-limit state; preserve it for diagnosis before retrying")
        # A backwards clock must not discard previously charged requests.
        if now < state["last_seen"]:
            state["requests"] = [now] * len(state["requests"])
            state["blocked_until"] = max(state["blocked_until"], now + WINDOW + MARGIN)
        state["requests"] = sorted(t for t in state["requests"] if t + WINDOW + MARGIN > now)
        state["last_seen"] = now
        return state

    def acquire(self) -> bool:
        """Wait as long as needed, then durably reserve one request. Return if waited."""
        waited = False
        announced = False
        while True:
            with self.store.exclusive_lock(self.scope):
                now = self.clock()
                state = self._load(now)
                until = state["blocked_until"]
                if len(state["requests"]) >= LIMIT:
                    until = max(until, state["requests"][0] + WINDOW + MARGIN)
                delay = max(0.0, until - now)
                if not delay:
                    state["requests"].append(now)
                self.store._atomic_json(self.path, state)
            if not delay:
                return waited
            if not announced:
                self.notify(f"YNAB rate limit: waiting about {math.ceil(delay)} seconds; will continue automatically.")
                announced = True
            waited = True
            # Interruptible and bounded; re-read state because other processes may extend cooldown.
            self.sleep(min(delay, 30.0))

    def observe(self, status: int, headers: Mapping[str, str]) -> None:
        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
        with self.store.exclusive_lock(self.scope):
            now = self.clock()
            state = self._load(now)
            count = re.fullmatch(r"\s*([0-9]{1,10})\s*/\s*([0-9]{1,10})\s*", normalized.get("x-rate-limit", ""))
            if count and int(count[2]) == LIMIT:
                used = min(int(count[1]), LIMIT)
                # Preserve known timestamps; add only otherwise-unaccounted usage.
                state["requests"].extend([now] * max(0, used - len(state["requests"])))
            if status == 429:
                # YNAB omits X-Rate-Limit on 429. Without a valid Retry-After,
                # waiting a full rolling hour avoids an unbounded probe loop.
                delay = WINDOW
                retry = normalized.get("retry-after", "").strip()
                if re.fullmatch(r"[0-9]{1,10}", retry):
                    delay = max(1.0, float(retry))
                elif retry:
                    try:
                        parsed = parsedate_to_datetime(retry)
                        if parsed.tzinfo is not None:
                            delay = max(1.0, parsed.timestamp() - now)
                    except (ValueError, OverflowError, TypeError):
                        pass
                state["blocked_until"] = max(state["blocked_until"], now + delay + MARGIN)
            self.store._atomic_json(self.path, state)
