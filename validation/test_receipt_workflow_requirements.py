# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Offline regression checks for the original validation findings.

Run explicitly with: python3 validation/test_receipt_workflow_requirements.py
Uses synthetic records and never reads Keychain or calls the live YNAB API.
Kept outside tests/ so it does not change the existing suite's result.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.response
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from budget_tools.api import ApiError, YnabClient, _default_transport
from budget_tools.cli import main, cmd_drafts, _milliunits, CliError
from budget_tools.state import StateStore, TERMS_VERSION
from tests.test_budget_tools import FakeClient


class CountingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.ignore_category = False
        self.change_total = False
        self.fail_write = False
        self.fail_read_after_write = False

    def get_transaction(self, plan_id, transaction_id):
        if self.fail_read_after_write and self.writes:
            raise ApiError('Synthetic verification timeout')
        return copy.deepcopy(self.transaction)

    def create_transaction(self, plan_id, transaction):
        self.writes += 1
        if self.fail_write:
            raise ApiError('Synthetic write timeout')
        self.transaction.update(copy.deepcopy(transaction))
        return {'data': {'transaction': copy.deepcopy(self.transaction)}}

    def update_transaction(self, plan_id, transaction_id, transaction):
        self.writes += 1
        if self.fail_write:
            raise ApiError('Synthetic write timeout')
        changes = copy.deepcopy(transaction)
        if self.ignore_category:
            changes.pop('category_id', None)
        self.transaction.update(changes)
        if self.change_total:
            self.transaction['amount'] = -99999
        return {'data': {'transaction': copy.deepcopy(self.transaction)}}


class WorkflowChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.store = StateStore(Path(self.temporary.name))
        self.store.save_config({'terms_acknowledged_version': TERMS_VERSION,
                               'plan_id': '99999999-9999-9999-9999-999999999999'})
        self.client = CountingClient()

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('budget_tools.cli._client', return_value=self.client):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(args, store=self.store)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def update_draft(self, *args):
        code, result, error = self.run_cli(['transactions', 'draft-update',
                                          self.client.transaction['id'], *args])
        self.assertEqual(code, 0, error)
        return result['id']

    def create_draft(self):
        code, result, error = self.run_cli(['transactions', 'draft-create', '--account', 'Example Account A',
                                          '--date', '2001-01-30', '--outflow', '43.25'])
        self.assertEqual(code, 0, error)
        return result['id']

    def apply(self, draft_id):
        return self.run_cli(['drafts', 'apply', draft_id, '--confirm', draft_id])

    def test_category_and_approval_cannot_share_a_generic_update(self):
        code, _, error = self.run_cli([
            'transactions', 'draft-update', self.client.transaction['id'],
            '--category', 'Example Group A: Example Category A', '--memo',
            'Synthetic receipt checked', '--approved',
        ])
        self.assertEqual(code, 1)
        self.assertIn('Approval must be drafted separately', error)
        self.assertEqual(self.client.writes, 0)

    def test_wrong_confirmation_prevents_write(self):
        draft_id = self.update_draft('--memo', 'Synthetic')
        code, _, _ = self.run_cli(['drafts', 'apply', draft_id, '--confirm', 'wrong'])
        self.assertEqual(code, 1)
        self.assertEqual(self.client.writes, 0)

    def test_expired_draft_prevents_write(self):
        draft_id = self.update_draft('--memo', 'Synthetic')
        # Advance the clock instead of modifying immutable reviewed content.
        with patch('budget_tools.cli.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=25)
            code, _, error = self.apply(draft_id)
        self.assertEqual(code, 1)
        self.assertIn('older than 24 hours', error)
        self.assertEqual(self.client.writes, 0)

    def test_successful_apply_cannot_be_repeated_sequentially(self):
        draft_id = self.create_draft()
        self.assertEqual(self.apply(draft_id)[0], 0)
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.client.writes, 1)

    def test_write_timeout_blocks_retry(self):
        draft_id = self.create_draft()
        self.client.fail_write = True
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.store.load_draft(draft_id)['status'], 'uncertain')
        self.assertEqual(self.apply(draft_id)[0], 1)
        self.assertEqual(self.client.writes, 1)

    def test_split_total_change_blocked(self):
        self.client.transaction['subtransactions'] = [{'amount': -43250}]
        code, _, _ = self.run_cli(['transactions', 'draft-update',
                                  self.client.transaction['id'], '--outflow', '50.00'])
        self.assertEqual(code, 1)
        self.assertEqual(self.client.writes, 0)

    def test_duplicate_category_names_rejected(self):
        groups = self.client.get_categories(self.client.plan_id)
        second = copy.deepcopy(groups[0])
        second['name'] = 'Other group'
        second['categories'][0]['id'] = '44444444-4444-4444-4444-444444444444'
        self.client.get_categories = lambda _: [groups[0], second]
        code, _, error = self.run_cli(['transactions', 'draft-update',
                                      self.client.transaction['id'], '--category', 'Example Category A'])
        self.assertEqual(code, 1)
        self.assertIn('Multiple categories', error)
        self.assertEqual(self.client.writes, 0)

    def test_categories_include_hidden_and_exclude_deleted(self):
        groups = self.client.get_categories(self.client.plan_id)
        hidden = copy.deepcopy(groups[0]['categories'][0])
        hidden.update(id='44444444-4444-4444-4444-444444444444', hidden=True)
        deleted = copy.deepcopy(hidden)
        deleted.update(id='55555555-5555-5555-5555-555555555555', deleted=True)
        groups[0]['categories'].extend([hidden, deleted])
        self.client.get_categories = lambda _: groups
        code, records, error = self.run_cli(['categories'])
        self.assertEqual(code, 0, error)
        self.assertEqual(len(records), 3)
        self.assertTrue(records[2]['hidden'])

    def test_invalid_amounts_rejected(self):
        for value in ('NaN', 'Infinity', '-1', '0.0001', 'not money'):
            with self.subTest(value=value), self.assertRaises(CliError):
                _milliunits(value, outflow=True)

    def test_missing_terms_blocks_api_access(self):
        self.store.save_config({})
        code, _, error = self.run_cli(['transactions', 'get', self.client.transaction['id']])
        self.assertEqual(code, 1)
        self.assertIn('not been acknowledged', error)
        self.assertEqual(self.client.writes, 0)

    # Preserve regressions from the original review; these requirements now pass.
    def test_split_get_exposes_category_amount_and_memo(self):
        self.client.transaction.update(category_id=None, subtransactions=[
            {'id': 'split-1', 'amount': -30000, 'category_id': 'category-1',
             'category_name': 'Example Category A', 'memo': 'Example Category C'},
            {'id': 'split-2', 'amount': -13250, 'category_id': 'category-2',
             'category_name': 'Example Category B', 'memo': 'Soap'},
        ])
        code, result, error = self.run_cli(['transactions', 'get', self.client.transaction['id']])
        self.assertEqual(code, 0, error)
        self.assertIn('subtransactions', result,
                      'Exact transaction read drops every split amount, category and memo')

    def test_payment_category_is_rejected_before_any_write(self):
        # YNAB documents that Credit Card Payment categories are ignored.
        groups = self.client.get_categories(self.client.plan_id)
        groups.append({'name': 'Credit Card Payments', 'categories': [
            {'id': '44444444-4444-4444-4444-444444444444', 'name': 'Example Account A',
             'hidden': False, 'deleted': False}
        ]})
        self.client.get_categories = lambda _: groups
        self.client.ignore_category = True
        code, _, error = self.run_cli([
            'transactions', 'draft-update', self.client.transaction['id'],
            '--category', 'Credit Card Payments: Example Account A', '--approved',
        ])
        self.assertEqual(code, 1)
        self.assertIn('cannot be assigned', error)
        self.assertFalse(self.client.transaction['approved'],
                         'Rejected category must not be approved')

    def test_generic_memo_plus_approval_is_rejected_before_drafting(self):
        code, _, error = self.run_cli([
            'transactions', 'draft-update', self.client.transaction['id'],
            '--memo', 'Synthetic receipt', '--approved',
        ])
        self.assertEqual(code, 1)
        self.assertIn('Approval must be drafted separately', error)

    def test_readback_failure_preserves_written_transaction_id(self):
        draft_id = self.create_draft()
        self.client.fail_read_after_write = True
        code, _, _ = self.apply(draft_id)
        self.assertEqual(code, 1)
        draft = self.store.load_draft(draft_id)
        self.assertEqual(draft.get('transaction_id'), self.client.transaction['id'],
                         'Successful create ID is lost when the verification GET fails')

    def test_concurrent_apply_submits_only_one_create(self):
        draft_id = self.create_draft()
        args = argparse.Namespace(drafts_command='apply', draft_id=draft_id, confirm=draft_id)
        def apply_once():
            try:
                cmd_drafts(args, self.store)
            except CliError:
                pass
        with patch('budget_tools.cli._client', return_value=self.client):
            with patch('budget_tools.cli._json_output'):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(apply_once) for _ in range(2)]
                    for future in futures:
                        future.result(timeout=10)
        self.assertEqual(self.client.writes, 1,
                         'Two applications both read pending and each issue a create request')


class TransportChecks(unittest.TestCase):
    def test_cross_origin_redirect_does_not_forward_authorization(self):
        requests = []
        class OfflineHTTPSHandler(urllib.request.BaseHandler):
            handler_order = 100
            def https_open(self, request):
                requests.append(request)
                headers = Message()
                if len(requests) == 1:
                    headers['Location'] = 'https://redirect-test.invalid/synthetic'
                    code, body = 302, b''
                else:
                    code, body = 200, b'{"data":{"user":{"id":"synthetic"}}}'
                response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, code)
                response.msg = 'Found' if code == 302 else 'OK'
                return response
        real_build_opener = urllib.request.build_opener
        def offline_opener(*handlers):
            return real_build_opener(
                urllib.request.ProxyHandler({}), OfflineHTTPSHandler(), *handlers
            )
        with patch('urllib.request.build_opener', side_effect=offline_opener):
            with self.assertRaises(ApiError):
                _default_transport(
                    'GET', 'https://api.ynab.com/v1/user',
                    {'Authorization': 'Bearer SYNTHETIC-TEST-CREDENTIAL'}, None, 1.0,
                )
        leaked = [request for request in requests
                  if request.full_url.startswith('https://redirect-test.invalid/')
                  and request.has_header('Authorization')]
        self.assertFalse(bool(leaked),
                         'Default transport forwards Authorization to a different HTTPS origin')
        self.assertEqual(len(requests), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
