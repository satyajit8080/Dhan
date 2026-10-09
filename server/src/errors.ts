/**
 * Typed exception hierarchy.
 *
 * Every error carries a stable `code` so the MCP layer can render something
 * actionable instead of a stack trace, and so retry logic can branch on the
 * kind of failure rather than on message text.
 */

export type ErrorCode =
  | 'CONFIG'
  | 'NO_TOKEN'
  | 'TOKEN_EXPIRED'
  | 'AUTH_REJECTED'
  | 'RATE_LIMITED'
  | 'DAILY_QUOTA'
  | 'TRANSPORT'
  | 'API'
  | 'INSTRUMENT'
  | 'VALIDATION'
  | 'INTEGRITY'
  | 'GATE_BLOCKED'
  | 'SNAPSHOT_SKEW';

export class Bull50DhanError extends Error {
  readonly code: ErrorCode;
  readonly details: Record<string, unknown>;
  /** True when a retry with backoff could plausibly succeed. */
  readonly retryable: boolean;

  constructor(
    code: ErrorCode,
    message: string,
    details: Record<string, unknown> = {},
    retryable = false,
  ) {
    super(message);
    this.name = new.target.name;
    this.code = code;
    this.details = details;
    this.retryable = retryable;
  }

  toJSON() {
    return {
      error: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      ...this.details,
    };
  }
}

export class ConfigError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>) {
    super('CONFIG', message, details, false);
  }
}

/** No token has been supplied yet today. */
export class NoTokenError extends Bull50DhanError {
  constructor(message = 'No Dhan access token available. Run /dhan-token to supply one.') {
    super('NO_TOKEN', message, {}, false);
  }
}

/** A token is present but its `exp` has passed. Dhan tokens are ~24h. */
export class TokenExpiredError extends Bull50DhanError {
  constructor(expiredAtIso: string) {
    super(
      'TOKEN_EXPIRED',
      `Dhan access token expired at ${expiredAtIso}. Run /dhan-token with a fresh token.`,
      { expiredAt: expiredAtIso },
      false,
    );
  }
}

/** Dhan rejected the credentials (DH-901, 808, 810...). */
export class AuthRejectedError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>) {
    super('AUTH_REJECTED', message, details, false);
  }
}

/**
 * Throttled. Covers the whole throttle family, because the published Dhan docs
 * are not consistent about which code appears where:
 *   - RL001 (named in the integration brief)
 *   - DH-904 "Rate Limit exceeded"
 *   - HTTP 429
 */
export class RateLimitError extends Bull50DhanError {
  readonly retryAfterMs: number;
  constructor(message: string, retryAfterMs: number, details?: Record<string, unknown>) {
    super('RATE_LIMITED', message, { ...details, retryAfterMs }, true);
    this.retryAfterMs = retryAfterMs;
  }
}

/** Local daily budget exhausted. Not a server response — a self-imposed stop. */
export class DailyQuotaError extends Bull50DhanError {
  constructor(used: number, limit: number) {
    super(
      'DAILY_QUOTA',
      `Local daily request budget exhausted (${used}/${limit}). ` +
        `Raise DHAN_DAILY_QUOTA if your plan allows more.`,
      { used, limit },
      false,
    );
  }
}

export class TransportError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>, retryable = true) {
    super('TRANSPORT', message, details, retryable);
  }
}

/** A structured non-2xx or status!=success response from Dhan. */
export class ApiError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>, retryable = false) {
    super('API', message, details, retryable);
  }
}

export class InstrumentError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>) {
    super('INSTRUMENT', message, details, false);
  }
}

export class ValidationError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>) {
    super('VALIDATION', message, details, false);
  }
}

/** Normalized data failed a structural or staleness check. */
export class IntegrityError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>) {
    super('INTEGRITY', message, details, false);
  }
}

/** The P0 pricing gate refused to publish. Carries every reason. */
export class GateBlockedError extends Bull50DhanError {
  readonly reasons: string[];
  constructor(reasons: string[], details?: Record<string, unknown>) {
    super(
      'GATE_BLOCKED',
      `Pricing gate BLOCKED — nothing published. ${reasons.length} reason(s): ` +
        reasons.join(' | '),
      { ...details, reasons },
      false,
    );
    this.reasons = reasons;
  }
}

/**
 * attach_pricing refused: the chain and the futures leg did not come from one
 * atomic snapshot. Never downgrade this to a warning.
 */
export class SnapshotSkewError extends Bull50DhanError {
  constructor(message: string, details?: Record<string, unknown>) {
    super('SNAPSHOT_SKEW', message, details, false);
  }
}
