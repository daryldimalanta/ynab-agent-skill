# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Daryl Dimalanta

"""Mutation runners fail closed on missed faults, setup errors and bad controls."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from validation import audit_mutation_checks, audit_new_payee_checks, audit_transaction_read_checks, audit_budget_planning_checks


class MutationGateTests(unittest.TestCase):
    def reject(self, control, mutated):
        for module in (audit_mutation_checks, audit_new_payee_checks, audit_transaction_read_checks, audit_budget_planning_checks):
            with self.subTest(runner=module.__name__):
                outcomes = iter([control])
                def fake_run(names):
                    return next(outcomes, mutated)
                with patch.object(module, "run", side_effect=fake_run), redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        module.main()
                self.assertNotEqual(caught.exception.code, 0)

    @staticmethod
    def result(**changes):
        value = {"tests": 1, "failures": 0, "errors": 0, "failure_count": 0, "detected": False}
        value.update(changes)
        return value

    def test_missed_fault_fails_gate(self):
        self.reject(self.result(), self.result())

    def test_error_is_not_a_detected_fault(self):
        self.reject(self.result(), self.result(errors=1))

    def test_errors_invalidate_other_assertion_failures(self):
        self.reject(self.result(), self.result(failures=1, failure_count=1, detected=True, errors=1))

    def test_failing_control_invalidates_experiment(self):
        self.reject(self.result(failures=1, failure_count=1, detected=True), self.result())

    def test_control_error_invalidates_experiment(self):
        self.reject(self.result(errors=1), self.result())

    def test_empty_control_invalidates_experiment(self):
        self.reject(self.result(tests=0), self.result())


class PatchEverywhereTests(unittest.TestCase):
    """Moved helpers must be replaced in every module that calls them."""

    def test_replaces_and_restores_every_binding(self):
        from budget_tools import cli, transactions
        from validation.mutation_support import patch_everywhere
        original = transactions._receipt_verification
        self.assertIs(cli._receipt_verification, original)
        marker = object()
        with patch_everywhere("_receipt_verification", marker):
            self.assertIs(cli._receipt_verification, marker)
            self.assertIs(transactions._receipt_verification, marker)
        self.assertIs(cli._receipt_verification, original)
        self.assertIs(transactions._receipt_verification, original)

    def test_unknown_symbol_is_rejected(self):
        from validation.mutation_support import patch_everywhere
        with self.assertRaisesRegex(AttributeError, "_not_a_workflow_helper"):
            with patch_everywhere("_not_a_workflow_helper", None):
                pass

    def test_divergent_bindings_are_rejected(self):
        from budget_tools import cli
        from validation.mutation_support import patch_everywhere
        with patch.object(cli, "_receipt_verification", object()):
            with self.assertRaisesRegex(AssertionError, "different objects"):
                with patch_everywhere("_receipt_verification", None):
                    pass
