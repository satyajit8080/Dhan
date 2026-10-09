"""Typed errors mirroring server/src/errors.ts (only those the ported code raises)."""


class Bull50Error(Exception):
    error_code = "ERROR"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class PricingValidationError(Bull50Error):
    """TS ValidationError (e.g. expired contract, T <= 0)."""
    error_code = "VALIDATION"


class SnapshotSkewError(Bull50Error):
    error_code = "SNAPSHOT_SKEW"


class GateBlockedError(Bull50Error):
    error_code = "GATE_BLOCKED"

    def __init__(self, reasons: list, details: dict | None = None):
        super().__init__(
            "Pricing gate BLOCKED — nothing published. %d reason(s): %s" % (len(reasons), " | ".join(reasons)),
            {**(details or {}), "reasons": reasons},
        )
        self.reasons = reasons
