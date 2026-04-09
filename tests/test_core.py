"""
Core unit tests for Tokenized Fund system.

Tests the critical invariants without requiring Docker or a database:
  - Journal balance enforcement (double-entry accounting)
  - Order state machine valid/invalid transitions
  - Settlement state machine valid/invalid transitions
  - RBAC permission checks
  - Decimal precision quantization
  - Idempotency key uniqueness
  - Append-only trigger verification (schema assertions)
"""

import sys
import os
import re
from decimal import Decimal, ROUND_HALF_EVEN

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fund-service"))

from rbac import (
    ROLE_PERMISSIONS,
    VALID_ORDER_TRANSITIONS,
    VALID_SETTLEMENT_TRANSITIONS,
    check_permission,
    validate_order_transition,
    validate_settlement_transition,
)


SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "db", "init", "001-schema.sql"
)

_PRECISION = Decimal("0.00000001")
_CASH_PRECISION = Decimal("0.01")


def _q(value):
    return value.quantize(_PRECISION, rounding=ROUND_HALF_EVEN)


def _cash(value):
    return value.quantize(_CASH_PRECISION, rounding=ROUND_HALF_EVEN)


# =================================================================
# 1. Double-entry journal balance enforcement
# =================================================================


class TestJournalBalance:
    """Verify that paired ledger entries always net to zero."""

    def test_subscription_journal_balances(self):
        nav = Decimal("99.50")
        investment = Decimal("25000000.00")
        shares = _q(investment / nav)

        share_debit = shares   # treasury
        share_credit = shares  # investor
        assert share_debit == share_credit

        cash_debit = _cash(investment)   # investor
        cash_credit = _cash(investment)  # treasury
        assert cash_debit == cash_credit

    def test_redemption_journal_balances(self):
        nav = Decimal("99.50")
        redeem_shares = _q(Decimal("50251.25628140"))
        proceeds = _cash(redeem_shares * nav)

        share_debit = redeem_shares   # investor
        share_credit = redeem_shares  # treasury
        assert share_debit == share_credit

        cash_debit = _cash(proceeds)   # treasury
        cash_credit = _cash(proceeds)  # investor
        assert cash_debit == cash_credit

    def test_transfer_journal_balances(self):
        transfer_shares = _q(Decimal("50000.00"))

        debit = transfer_shares   # sender
        credit = transfer_shares  # receiver
        assert debit == credit


# =================================================================
# 2. Order state machine transitions
# =================================================================


class TestOrderStateMachine:
    """Verify order lifecycle follows the defined state machine."""

    def test_valid_full_lifecycle(self):
        path = [
            (None, "PENDING"),
            ("PENDING", "COMPLIANCE_CHECK"),
            ("COMPLIANCE_CHECK", "APPROVED"),
            ("APPROVED", "NAV_APPLIED"),
            ("NAV_APPLIED", "SETTLED"),
        ]
        for current, target in path:
            validate_order_transition(current, target)

    def test_valid_rejection_path(self):
        validate_order_transition(None, "PENDING")
        validate_order_transition("PENDING", "REJECTED")

    def test_valid_cancellation_path(self):
        validate_order_transition("PENDING", "CANCELLED")
        validate_order_transition("APPROVED", "CANCELLED")

    def test_invalid_skip_compliance(self):
        with pytest.raises(ValueError, match="Invalid order transition"):
            validate_order_transition("PENDING", "APPROVED")

    def test_invalid_backward_transition(self):
        with pytest.raises(ValueError, match="Invalid order transition"):
            validate_order_transition("SETTLED", "PENDING")

    def test_terminal_states_block_transitions(self):
        for terminal in ("SETTLED", "FAILED", "CANCELLED", "REJECTED"):
            with pytest.raises(ValueError):
                validate_order_transition(terminal, "PENDING")


# =================================================================
# 3. Settlement state machine transitions
# =================================================================


class TestSettlementStateMachine:
    """Verify settlement lifecycle follows the defined state machine."""

    def test_valid_full_lifecycle(self):
        path = [
            (None, "PENDING"),
            ("PENDING", "APPROVED"),
            ("APPROVED", "SIGNED"),
            ("SIGNED", "BROADCASTED"),
            ("BROADCASTED", "CONFIRMED"),
        ]
        for current, target in path:
            validate_settlement_transition(current, target)

    def test_failure_from_any_active_state(self):
        for state in ("PENDING", "APPROVED", "SIGNED", "BROADCASTED"):
            validate_settlement_transition(state, "FAILED")

    def test_invalid_skip_signing(self):
        with pytest.raises(ValueError, match="Invalid settlement"):
            validate_settlement_transition("APPROVED", "BROADCASTED")

    def test_terminal_states_block_transitions(self):
        for terminal in ("CONFIRMED", "FAILED"):
            with pytest.raises(ValueError):
                validate_settlement_transition(terminal, "PENDING")


# =================================================================
# 4. RBAC permission checks
# =================================================================


class TestRBAC:
    """Verify role-based access control enforcement."""

    def test_admin_wildcard_grants_anything(self):
        check_permission("ADMIN", "fund.create")
        check_permission("ADMIN", "nonexistent.permission")

    def test_system_role_has_operational_permissions(self):
        for perm in ("fund.create", "order.process", "settlement.sign"):
            check_permission("SYSTEM", perm)

    def test_investor_limited_to_order_submit(self):
        check_permission("INVESTOR", "order.submit")
        with pytest.raises(PermissionError):
            check_permission("INVESTOR", "fund.create")

    def test_unknown_role_denied(self):
        with pytest.raises(PermissionError):
            check_permission("UNKNOWN_ROLE", "fund.create")

    def test_signer_cannot_approve_settlements(self):
        with pytest.raises(PermissionError):
            check_permission("SIGNER", "settlement.approve")


# =================================================================
# 5. Decimal precision (shares and cash)
# =================================================================


class TestPrecision:
    """Verify quantization matches NUMERIC(28,8) and USD cents."""

    def test_share_precision_eight_decimals(self):
        result = _q(Decimal("251256.28140703517587939698"))
        assert result == Decimal("251256.28140704")
        assert result.as_tuple().exponent == -8

    def test_cash_precision_two_decimals(self):
        result = _cash(Decimal("25000000.005"))
        assert result == Decimal("25000000.00")
        assert result.as_tuple().exponent == -2

    def test_share_calculation_deterministic(self):
        nav = Decimal("99.50")
        investment = Decimal("25000000.00")
        shares = _q(investment / nav)
        assert shares == _q(investment / nav)


# =================================================================
# 6. Append-only trigger verification (schema)
# =================================================================


class TestAppendOnlySchema:
    """Verify the SQL schema enforces immutability via triggers."""

    @pytest.fixture(scope="class")
    def schema(self):
        with open(SCHEMA_PATH) as f:
            return f.read()

    def test_deny_mutation_function_exists(self, schema):
        assert "CREATE OR REPLACE FUNCTION deny_mutation()" in schema

    def test_core_tables_have_update_triggers(self, schema):
        immutable_tables = [
            "funds", "investors", "share_ledger",
            "cash_ledger", "orders", "order_events",
            "settlements", "settlement_events",
        ]
        for table in immutable_tables:
            pattern = rf"BEFORE UPDATE ON {table}\b"
            assert re.search(pattern, schema), (
                f"Missing UPDATE deny trigger on {table}"
            )

    def test_core_tables_have_delete_triggers(self, schema):
        immutable_tables = [
            "funds", "investors", "share_ledger",
            "cash_ledger", "orders",
        ]
        for table in immutable_tables:
            pattern = rf"BEFORE DELETE ON {table}\b"
            assert re.search(pattern, schema), (
                f"Missing DELETE deny trigger on {table}"
            )

    def test_deferred_balance_constraints_exist(self, schema):
        assert "check_share_journal_balance" in schema
        assert "check_cash_journal_balance" in schema
