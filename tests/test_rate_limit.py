# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Synthetic clocks and offline transports exercise hours of work in seconds."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from budget_tools.api import ApiError, YnabClient, _default_transport
from budget_tools.rate_limit import LIMIT, WINDOW, MARGIN, RateLimiter
from budget_tools.state import StateError, StateStore, TERMS_VERSION
from tests import test_receipt_contract_workflow as contract
from tests.test_receipt_contract_workflow import ContractServer, PLAN, TX, raw_proposal
from validation.test_post_implementation_safety import OfflineCase


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.sleeps = []
        self.on_sleep = lambda: None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.on_sleep()
        self.now += seconds


class RateLimitTests(OfflineCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root / "state")
        self.clock = Clock()
        self.messages = []
        self.limiter = self.make_limiter()

    def make_limiter(self, token="SYNTHETIC-LIMIT-TOKEN"):
        return RateLimiter(self.store, token, clock=self.clock, sleep=self.clock.sleep,
                           notify=self.messages.append)

    def seed(self, *, count=0, blocked=0):
        self.store.ensure()
        self.store._atomic_json(self.limiter.path, {
            "version": 1, "requests": [self.clock.now] * count,
            "blocked_until": blocked, "last_seen": self.clock.now,
        })

    def test_601_requests_obey_every_rolling_window_over_three_hours(self):
        sent = []
        for _ in range(601):
            self.limiter.acquire()
            sent.append(self.clock.now)
        for t in sent:
            self.assertLessEqual(sum(t - WINDOW < s <= t for s in sent), LIMIT)
        self.assertGreaterEqual(sent[-1] - sent[0], 3 * (WINDOW + MARGIN))
        self.assertTrue(all(0 < s <= 30 for s in self.clock.sleeps))
        self.assertEqual(len(self.messages), 3)

    def test_restart_and_separate_instances_share_usage(self):
        self.seed(count=200)
        self.assertTrue(self.make_limiter().acquire())
        self.assertEqual(self.clock.now, 1_800_003_601)
        self.assertEqual(len(self.store._read_json(self.limiter.path)["requests"]), 1)

    def test_exact_rolling_boundary_and_staggered_slots(self):
        self.seed(count=199)
        self.clock.now += 100
        self.limiter.acquire()
        self.clock.now += WINDOW + MARGIN - 100
        self.assertFalse(self.limiter.acquire())
        state = self.store._read_json(self.limiter.path)
        self.assertEqual(len(state["requests"]), 2)
        self.assertEqual(self.clock.sleeps, [])

    def test_new_client_keeps_shared_server_cooldown(self):
        self.limiter.observe(429, {"Retry-After": "600"})
        self.make_limiter().observe(200, {"X-Rate-Limit": "1/200"})
        self.assertTrue(self.make_limiter().acquire())
        self.assertEqual(sum(self.clock.sleeps), 601)

    def test_cli_attaches_persistent_limiter(self):
        from budget_tools.cli import _client
        with patch("budget_tools.cli.read_access_token", return_value="SYNTHETIC-LIMIT-TOKEN"), \
             patch("budget_tools.cli.StateStore", return_value=self.store):
            client = _client()
        self.assertIsInstance(client._rate_limiter, RateLimiter)
        self.assertEqual(client._rate_limiter.path, self.limiter.path)

    def test_tokens_have_independent_private_buckets(self):
        self.seed(count=200)
        other = self.make_limiter("OTHER-SYNTHETIC-TOKEN")
        self.assertFalse(other.acquire())
        self.assertNotEqual(self.limiter.path, other.path)
        self.assertNotIn("SYNTHETIC", other.path.name)
        self.assertEqual(other.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(self.store._read_json(other.path)),
                         {"version", "requests", "blocked_until", "last_seen"})

    def test_simultaneous_instances_do_not_lose_reservations(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.make_limiter().acquire(), range(80)))
        self.assertFalse(any(results))
        self.assertEqual(len(self.store._read_json(self.limiter.path)["requests"]), 80)

    def test_separate_processes_share_accounting(self):
        self.seed()
        script = """from pathlib import Path
from budget_tools.state import StateStore
from budget_tools.rate_limit import RateLimiter
import sys
r = RateLimiter(StateStore(Path(sys.argv[1])), 'SYNTHETIC-LIMIT-TOKEN', clock=lambda: 1_800_000_000.0)
for _ in range(10): r.acquire()
"""
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.store.root)])
                    for _ in range(4)]
        try:
            for child in children:
                self.assertEqual(child.wait(timeout=15), 0)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
        self.assertEqual(len(self.store._read_json(self.limiter.path)["requests"]), 40)

    def test_lock_released_while_sleeping(self):
        self.seed(count=200)
        def during_sleep():
            self.clock.on_sleep = lambda: None
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(self.make_limiter().observe, 200, {})
                result.result(timeout=3)
        self.clock.on_sleep = during_sleep
        self.limiter.acquire()

    def test_header_accounts_for_external_usage_without_lowering_local_count(self):
        self.seed(count=5)
        self.limiter.observe(200, {"X-Rate-Limit": "199/200"})
        self.limiter.observe(200, {"x-rate-limit": "2/200"})
        self.assertFalse(self.limiter.acquire())
        self.assertTrue(self.limiter.acquire())

    def test_missing_or_invalid_headers_do_not_reset_accounting(self):
        self.seed(count=200)
        for value in ("", "nonsense", "1/0", "1/201", "-1/200"):
            self.limiter.observe(200, {"X-Rate-Limit": value})
        self.assertTrue(self.limiter.acquire())

    def test_429_headerless_or_malformed_waits_a_full_hour(self):
        for value in ("", "nonsense", "-1", "NaN", "1.5"):
            with self.subTest(value=value):
                self.seed()
                before = self.clock.now
                self.limiter.observe(429, {"Retry-After": value})
                self.assertTrue(self.make_limiter().acquire())
                self.assertEqual(self.clock.now - before, WINDOW + MARGIN)

    def test_retry_after_seconds_date_zero_and_past_date(self):
        for value, expected in (("45", 46), ("0", 2),
                (format_datetime(datetime.fromtimestamp(self.clock.now + 90, timezone.utc)), 91),
                ("Wed, 01 Jan 2020 00:00:00 GMT", 2)):
            with self.subTest(value=value):
                # Fixed base for the date fixture.
                self.clock.now = 1_800_000_000
                self.seed()
                self.limiter.observe(429, {"rEtRy-AfTeR": value})
                self.limiter.acquire()
                self.assertEqual(self.clock.now - 1_800_000_000, expected)

    def test_persisted_cooldown_survives_interruption(self):
        self.limiter.observe(429, {})
        self.clock.on_sleep = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.limiter.acquire()
        self.clock.on_sleep = lambda: None
        self.assertTrue(self.make_limiter().acquire())

    def test_backward_clock_preserves_capacity_guard(self):
        self.seed(count=20)
        self.clock.now -= 120
        before = self.clock.now
        self.assertTrue(self.limiter.acquire())
        self.assertGreaterEqual(self.clock.now - before, WINDOW + MARGIN)

    def test_corrupt_state_fails_closed(self):
        for state in ({}, {"version": 1, "requests": [float("nan")],
                          "blocked_until": 0, "last_seen": self.clock.now},
                      {"version": 1, "requests": [True], "blocked_until": 0,
                       "last_seen": self.clock.now}):
            with self.subTest(state=state):
                self.store.ensure()
                self.store._atomic_json(self.limiter.path, state)
                with self.assertRaises(StateError):
                    self.limiter.acquire()

    def test_unsafe_state_files_are_rejected(self):
        self.seed()
        self.limiter.path.chmod(0o644)
        with self.assertRaises(StateError):
            self.limiter.acquire()
        self.limiter.path.unlink()
        target = self.root / "target"
        target.write_text("{}")
        self.limiter.path.symlink_to(target)
        with self.assertRaises(StateError):
            self.limiter.acquire()

    def test_oversized_corrupt_timestamps_fail_closed(self):
        for bad in (10 ** 400, float("inf"), -1):
            self.seed()
            state = self.store._read_json(self.limiter.path)
            state["last_seen"] = bad
            self.store._atomic_json(self.limiter.path, state)
            with self.assertRaises(StateError):
                self.limiter.acquire()

    def test_extreme_usage_header_is_ignored_safely(self):
        self.limiter.observe(200, {"X-Rate-Limit": "9" * 10000 + "/200"})
        self.assertFalse(self.limiter.acquire())

    def test_persistence_failure_prevents_transport(self):
        client = YnabClient("SYNTHETIC", rate_limiter=self.limiter,
                            transport=lambda *args: self.fail("Sent before reservation persisted"))
        with patch.object(self.store, "_atomic_json", side_effect=StateError("synthetic disk failure")):
            with self.assertRaises(StateError):
                client.get_user()

    def test_repeated_429s_retry_get_put_post_without_human_input(self):
        for method in ("GET", "PUT", "POST"):
            with self.subTest(method=method):
                self.seed()
                calls = []
                def transport(*args):
                    calls.append(args)
                    return (429, b'{}', {}) if len(calls) <= 3 else (200, b'{"ok":true}', {})
                before = self.clock.now
                client = YnabClient("SYNTHETIC", transport=transport, rate_limiter=self.limiter)
                self.assertTrue(client.request(method, "/user")["ok"])
                self.assertEqual(len(calls), 4)
                self.assertEqual(self.clock.now - before, 3 * (WINDOW + MARGIN))

    def test_http_error_429_uses_headers_and_closes_body(self):
        body = io.BytesIO(b'{}')
        error = urllib.error.HTTPError("https://api.ynab.com/v1/user", 429, "Limited",
                                       {"Retry-After": "8"}, body)
        replies = [error, (200, b'{"data":{"user":{"id":"synthetic"}}}', {})]
        def transport(*args):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        client = YnabClient("SYNTHETIC", transport=transport, rate_limiter=self.limiter)
        self.assertEqual(client.get_user()["id"], "synthetic")
        self.assertTrue(body.closed)
        self.assertEqual(sum(self.clock.sleeps), 9)

    def test_other_errors_never_automatically_replay_writes(self):
        for reply in ((400, b'{}'), (401, b'{}'), (403, b'{}'), (500, b'{}'),
                      (503, b'{}'), (200, b'invalid'), TimeoutError(),
                      urllib.error.URLError("synthetic")):
            with self.subTest(reply=repr(reply)):
                calls = []
                def transport(*args):
                    calls.append(args)
                    if isinstance(reply, Exception):
                        raise reply
                    return reply
                client = YnabClient("SYNTHETIC", transport=transport, rate_limiter=self.limiter)
                with self.assertRaises(ApiError):
                    client.request("POST", "/plans/synthetic/transactions", body={})
                self.assertEqual(len(calls), 1)

    def test_default_transport_retains_headers(self):
        response = io.BytesIO(b'{}')
        response.status = 200
        response.headers = {"X-Rate-Limit": "33/200"}
        with patch("urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(_default_transport("GET", "https://api.ynab.com/v1/user", {}, None, 1),
                             (200, b'{}', {"X-Rate-Limit": "33/200"}))


class RateLimitedReceiptTests(OfflineCase):
    run_cli = contract.ReceiptContractWorkflow.run_cli
    ok = contract.ReceiptContractWorkflow.ok
    draft = contract.ReceiptContractWorkflow.draft
    save_proposal = contract.ReceiptContractWorkflow.save_proposal

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = StateStore(self.root / "state")
        self.store.save_config({"terms_acknowledged_version": TERMS_VERSION, "plan_id": PLAN})
        self.server = ContractServer()
        self.clock = Clock()
        self.limiter = RateLimiter(self.store, "SYNTHETIC", clock=self.clock,
                                   sleep=self.clock.sleep, notify=lambda _: None)
        self.transport = self.server.transport
        self.client = YnabClient("SYNTHETIC-AUDIT-ONLY", transport=lambda *args: self.transport(*args),
                                 rate_limiter=self.limiter)
        self.value = raw_proposal()
        self.path = self.root / "proposal.json"

    def apply(self, draft):
        return self.run_cli("drafts", "apply", draft["id"], "--confirm", draft["id"])

    def reject_once(self, predicate, *, after=None):
        rejected = []
        def transport(*args):
            if not rejected and predicate(args):
                rejected.append(True)
                if after:
                    after()
                return 429, b'{}', {}
            return self.server.transport(*args)
        self.transport = transport
        return rejected

    def test_split_and_approval_each_retry_after_an_hour_then_verify(self):
        draft = self.draft()
        rejections = set()
        def transport(*args):
            if args[0] == "PUT":
                kind = "approve" if json.loads(args[3])["transaction"] == {"approved": True} else "split"
                if kind not in rejections:
                    rejections.add(kind)
                    return 429, b'{}', {}
            return self.server.transport(*args)
        self.transport = transport
        code, result, error = self.apply(draft)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(len(self.server.writes()), 2)
        self.assertEqual(sum(self.clock.sleeps), 2 * (WINDOW + MARGIN))

    def test_changed_transaction_during_wait_blocks_conversion(self):
        draft = self.draft()
        self.reject_once(lambda args: args[0] == "PUT")
        self.clock.on_sleep = lambda: self.server.records[TX].update(memo="External change")
        code, _, error = self.apply(draft)
        self.assertEqual(code, 1)
        self.assertIn("changed", error)
        self.assertEqual(self.server.writes(), [])
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "pending")

    def test_proactive_wait_also_revalidates_before_write(self):
        draft = self.draft()
        first = []
        def transport(*args):
            result = self.server.transport(*args)
            if args[0] == "GET" and not first:
                first.append(True)
                return (*result, {"X-Rate-Limit": "200/200"})
            return result
        self.transport = transport
        self.clock.on_sleep = lambda: self.server.records[TX].update(amount=-99999)
        self.assertEqual(self.apply(draft)[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_approval_delay_rechecks_categorization(self):
        draft = self.draft()
        self.reject_once(lambda args: args[0] == "PUT" and json.loads(args[3])["transaction"] == {"approved": True})
        self.clock.on_sleep = lambda: self.server.records[TX].update(memo="Changed after categorization")
        self.assertEqual(self.apply(draft)[0], 1)
        self.assertEqual(len(self.server.writes()), 1)
        self.assertFalse(self.server.records[TX]["approved"])

    def test_readback_429_never_repeats_conversion(self):
        draft = self.draft()
        self.reject_once(lambda args: args[0] == "GET" and bool(self.server.writes()))
        self.assertEqual(self.apply(draft)[0], 0)
        self.assertEqual(len(self.server.writes()), 2)

    def test_interrupted_rejected_write_remains_safely_retryable(self):
        draft = self.draft()
        self.reject_once(lambda args: args[0] == "PUT")
        self.clock.on_sleep = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.apply(draft)[0], 130)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "pending")
        self.clock.on_sleep = lambda: None
        self.assertEqual(self.apply(draft)[0], 0)
        self.assertEqual(len(self.server.writes()), 2)

    def test_interrupted_approval_resumes_only_approval(self):
        draft = self.draft()
        self.reject_once(lambda args: args[0] == "PUT" and json.loads(args[3])["transaction"] == {"approved": True})
        self.clock.on_sleep = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.apply(draft)[0], 130)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "approval_pending")
        self.clock.on_sleep = lambda: None
        result = self.ok("drafts", "resume", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(len(self.server.writes()), 2)

    def test_interrupt_during_transport_does_not_allow_replay(self):
        draft = self.draft()
        def transport(*args):
            response = self.server.transport(*args)
            if args[0] == "PUT":
                raise KeyboardInterrupt()
            return response
        self.transport = transport
        self.assertEqual(self.apply(draft)[0], 130)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "applying")
        self.assertEqual(self.apply(draft)[0], 1)
        self.assertEqual(len(self.server.writes()), 1)
        self.transport = self.server.transport
        self.assertEqual(self.ok("drafts", "reconcile", draft["id"])["status"], "approval_pending")

    def test_interrupted_readback_keeps_known_id_and_never_rewrites(self):
        draft = self.draft()
        self.reject_once(lambda args: args[0] == "GET" and bool(self.server.writes()))
        self.clock.on_sleep = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.apply(draft)[0], 130)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "verification_pending")
        self.clock.on_sleep = lambda: None
        self.assertEqual(self.ok("drafts", "reconcile", draft["id"])["status"], "approval_pending")
        self.ok("drafts", "resume", draft["id"], "--confirm", draft["id"])
        self.assertEqual(len(self.server.writes()), 2)

    def test_wait_messages_do_not_corrupt_json_output(self):
        draft = self.draft()
        self.limiter.notify = lambda message: print(message, file=sys.stderr, flush=True)
        self.reject_once(lambda args: args[0] == "PUT")
        code, result, error = self.apply(draft)
        self.assertEqual(code, 0)
        self.assertEqual(result["stage"], "verified_approval")
        self.assertIn("will continue automatically", error)
        self.assertNotIn("SYNTHETIC", error)

    def test_legacy_definite_429_rejection_can_apply_same_authorized_draft(self):
        draft = self.draft()
        saved = self.store.load_draft(draft["id"])
        saved.update(status="failed", stage="write_rejected", error={"status": 429})
        self.store.save_draft(saved)
        self.assertEqual(self.apply(draft)[0], 0)
        self.assertEqual(len(self.server.writes()), 2)

    def test_legacy_nonrate_rejection_and_uncertainty_cannot_apply(self):
        draft = self.draft()
        for status, stage, error in (("failed", "write_rejected", {"status": 400}),
                                     ("uncertain", "write_uncertain", {"status": 429})):
            saved = self.store.load_draft(draft["id"])
            saved.update(status=status, stage=stage, error=error)
            self.store.save_draft(saved)
            self.assertEqual(self.apply(draft)[0], 1)
        self.assertEqual(self.server.writes(), [])

    def test_legacy_approval_429_retries_only_approval(self):
        draft = self.draft()
        def reject_approval(*args):
            if args[0] == "PUT" and json.loads(args[3])["transaction"] == {"approved": True}:
                return 400, b'{}'
            return self.server.transport(*args)
        self.transport = reject_approval
        self.assertEqual(self.apply(draft)[0], 1)
        saved = self.store.load_draft(draft["id"])
        self.assertEqual(saved["status"], "approval_failed")
        self.assertEqual(self.run_cli("drafts", "resume", draft["id"], "--confirm", draft["id"])[0], 1)
        saved["approval_error"]["status"] = 429
        self.store.save_draft(saved)
        self.transport = self.server.transport
        result = self.ok("drafts", "resume", draft["id"], "--confirm", draft["id"])
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual(len(self.server.writes()), 2)

    def test_expiration_during_wait_prevents_write(self):
        draft = self.draft()
        created = datetime.fromisoformat(self.store.load_draft(draft["id"])["created_at"])
        clock = self.clock
        start = clock.now
        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return created + timedelta(hours=23, minutes=30, seconds=clock.now - start)
        self.reject_once(lambda args: args[0] == "PUT")
        with patch("budget_tools.cli.datetime", FakeDatetime):
            code, _, error = self.apply(draft)
        self.assertEqual(code, 1)
        self.assertIn("24 hours", error)
        self.assertEqual(self.server.writes(), [])

    def test_creation_429_then_success_creates_exactly_once(self):
        self.value["transaction_id"] = None
        self.save_proposal()
        draft = self.ok("transactions", "draft-receipt-create", "--account", "Example Account B", "--payee", "Example Shop",
                        "--date", "2001-02-02", "--outflow", "28.80", "--proposal", str(self.path))
        self.reject_once(lambda args: args[0] == "POST")
        code, result, error = self.apply(draft)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["stage"], "verified_approval")
        self.assertEqual([call[0] for call in self.server.writes()], ["POST", "PUT"])

    def test_lost_success_after_429_never_replays_creation(self):
        self.value["transaction_id"] = None
        self.save_proposal()
        draft = self.ok("transactions", "draft-receipt-create", "--account", "Example Account B", "--payee", "Example Shop",
                        "--date", "2001-02-02", "--outflow", "28.80", "--proposal", str(self.path))
        rejected = []
        def transport(*args):
            if args[0] == "POST" and not rejected:
                rejected.append(True)
                return 429, b'{}', {}
            response = self.server.transport(*args)
            if args[0] == "POST":
                raise TimeoutError("Synthetic response lost after successful creation")
            return response
        self.transport = transport
        self.assertEqual(self.apply(draft)[0], 1)
        self.assertEqual(self.store.load_draft(draft["id"])["status"], "uncertain")
        self.assertEqual(self.apply(draft)[0], 1)
        self.assertEqual([call[0] for call in self.server.writes()], ["POST"])
