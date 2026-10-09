/**
 * Credentials, settings, and a logger that cannot leak a token.
 *
 * NO HARDCODED CREDENTIALS. The token is resolved at call time, never captured
 * at import time, so a token supplied mid-session takes effect immediately.
 *
 * Resolution order (first hit wins):
 *   1. in-memory  — set via the `set_dhan_token` MCP tool / `/dhan-token`
 *   2. env        — DHAN_ACCESS_TOKEN
 *   3. file       — DHAN_TOKEN_FILE, re-read on every access (hot reload)
 */

import { readFileSync, statSync } from 'node:fs';
import { ConfigError, NoTokenError, TokenExpiredError } from './errors.js';

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export interface Settings {
  baseUrl: string;
  /** Risk-free rate used for DF and Black-76. */
  riskFreeRate: number;
  /** Local per-day request budget across all limit classes. */
  dailyQuota: number;
  /** Max age of a leg before it is treated as stale, in ms. */
  maxQuoteAgeMs: number;
  /** Max allowed gap between the chain fetch and the futures fetch, in ms. */
  maxSnapshotSkewMs: number;
  /** Expiry list cache TTL, in ms. Expiries are never hardcoded. */
  expiryCacheTtlMs: number;
  /** Scrip-master cache TTL, in ms. */
  scripMasterTtlMs: number;
  logLevel: 'debug' | 'info' | 'warn' | 'error' | 'silent';
}

function num(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const v = Number(raw);
  if (!Number.isFinite(v)) throw new ConfigError(`${name} must be a number, got "${raw}"`);
  return v;
}

export function loadSettings(): Settings {
  const level = (process.env['DHAN_LOG_LEVEL'] ?? 'info') as Settings['logLevel'];
  return {
    baseUrl: process.env['DHAN_BASE_URL'] ?? 'https://api.dhan.co/v2',
    riskFreeRate: num('DHAN_RISK_FREE_RATE', 0.065),
    // The integration brief specifies 7000/day. Dhan's published figure for
    // Data APIs is far higher (~100k/day). We default to the conservative
    // number and log which ceiling is in force.
    dailyQuota: num('DHAN_DAILY_QUOTA', 7000),
    maxQuoteAgeMs: num('DHAN_MAX_QUOTE_AGE_MS', 15_000),
    maxSnapshotSkewMs: num('DHAN_MAX_SNAPSHOT_SKEW_MS', 3_000),
    expiryCacheTtlMs: num('DHAN_EXPIRY_TTL_MS', 6 * 60 * 60 * 1000),
    scripMasterTtlMs: num('DHAN_SCRIP_TTL_MS', 12 * 60 * 60 * 1000),
    logLevel: level,
  };
}

// ---------------------------------------------------------------------------
// Redacting logger
// ---------------------------------------------------------------------------

const JWT_RE = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+/g;
const BEARER_RE = /\b(access-token|authorization|token)\b(\s*[:=]\s*)(\S+)/gi;

/** Tokens seen this process, so we can redact them even in odd formats. */
const knownSecrets = new Set<string>();

export function registerSecret(secret: string): void {
  if (secret && secret.length >= 8) knownSecrets.add(secret);
}

/** Strip anything token-shaped from arbitrary text. Used on EVERY log line. */
export function redact(input: unknown): string {
  let s =
    typeof input === 'string'
      ? input
      : (() => {
          try {
            return JSON.stringify(input);
          } catch {
            return String(input);
          }
        })();

  for (const secret of knownSecrets) {
    if (secret.length >= 8) s = s.split(secret).join('[REDACTED]');
  }
  s = s.replace(JWT_RE, '[REDACTED_JWT]');
  s = s.replace(BEARER_RE, (_m, k: string, sep: string) => `${k}${sep}[REDACTED]`);
  return s;
}

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40, silent: 99 } as const;

export interface Logger {
  debug(msg: string, meta?: unknown): void;
  info(msg: string, meta?: unknown): void;
  warn(msg: string, meta?: unknown): void;
  error(msg: string, meta?: unknown): void;
}

/**
 * Logs to STDERR, always.
 *
 * An MCP stdio server speaks JSON-RPC on stdout; anything written there that is
 * not a protocol message corrupts the stream and the client disconnects.
 */
export function createLogger(settings: Settings): Logger {
  const min = LEVELS[settings.logLevel] ?? LEVELS.info;

  const emit = (level: keyof typeof LEVELS, msg: string, meta?: unknown) => {
    if (LEVELS[level] < min) return;
    const line =
      `[bull50-dhan] ${new Date().toISOString()} ${level.toUpperCase()} ${redact(msg)}` +
      (meta === undefined ? '' : ` ${redact(meta)}`);
    process.stderr.write(line + '\n');
  };

  return {
    debug: (m, x) => emit('debug', m, x),
    info: (m, x) => emit('info', m, x),
    warn: (m, x) => emit('warn', m, x),
    error: (m, x) => emit('error', m, x),
  };
}

// ---------------------------------------------------------------------------
// Token provider
// ---------------------------------------------------------------------------

export type TokenSource = 'memory' | 'env' | 'file';

export interface TokenStatus {
  present: boolean;
  source: TokenSource | null;
  /** Decoded from the JWT `exp` claim. Null when the token is not a JWT. */
  expiresAtIso: string | null;
  msUntilExpiry: number | null;
  expired: boolean;
  /** Last 4 characters only. Never the token. */
  fingerprint: string | null;
  clientId: string | null;
}

/** Decode a JWT payload WITHOUT verifying it. We are not the issuer. */
function decodeJwtExp(token: string): number | null {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(
      Buffer.from(parts[1]!.replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8'),
    ) as { exp?: number };
    return typeof payload.exp === 'number' ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

export class TokenProvider {
  private memoryToken: string | null = null;
  private memoryClientId: string | null = null;
  private fileCache: { path: string; mtimeMs: number; token: string } | null = null;

  constructor(private readonly log: Logger) {}

  /**
   * Supply a token for this process only.
   *
   * Never written to disk, never echoed back, and registered with the redactor
   * so it cannot appear in any subsequent log line.
   */
  setToken(token: string, clientId?: string): TokenStatus {
    const t = token.trim();
    if (t.length < 16) {
      throw new ConfigError('That does not look like a Dhan access token (too short).');
    }
    registerSecret(t);
    this.memoryToken = t;
    if (clientId) this.memoryClientId = clientId.trim();
    const status = this.status();
    this.log.info('Access token set from chat', {
      source: 'memory',
      expiresAt: status.expiresAtIso,
      fingerprint: status.fingerprint,
    });
    return status;
  }

  clear(): void {
    this.memoryToken = null;
    this.memoryClientId = null;
    this.log.info('Access token cleared from memory');
  }

  private resolve(): { token: string; source: TokenSource } | null {
    if (this.memoryToken) return { token: this.memoryToken, source: 'memory' };

    const env = process.env['DHAN_ACCESS_TOKEN'];
    if (env && env.trim() && !env.startsWith('${')) {
      const t = env.trim();
      registerSecret(t);
      return { token: t, source: 'env' };
    }

    const path = process.env['DHAN_TOKEN_FILE'];
    if (path) {
      try {
        const { mtimeMs } = statSync(path);
        if (this.fileCache?.path === path && this.fileCache.mtimeMs === mtimeMs) {
          return { token: this.fileCache.token, source: 'file' };
        }
        const t = readFileSync(path, 'utf8').trim();
        if (t) {
          registerSecret(t);
          this.fileCache = { path, mtimeMs, token: t };
          return { token: t, source: 'file' };
        }
      } catch {
        // Missing or unreadable token file is not fatal on its own.
      }
    }
    return null;
  }

  clientId(): string | null {
    const env = process.env['DHAN_CLIENT_ID'];
    if (this.memoryClientId) return this.memoryClientId;
    if (env && env.trim() && !env.startsWith('${')) return env.trim();
    return null;
  }

  status(): TokenStatus {
    const resolved = this.resolve();
    if (!resolved) {
      return {
        present: false,
        source: null,
        expiresAtIso: null,
        msUntilExpiry: null,
        expired: false,
        fingerprint: null,
        clientId: this.clientId(),
      };
    }
    const expMs = decodeJwtExp(resolved.token);
    const now = Date.now();
    return {
      present: true,
      source: resolved.source,
      expiresAtIso: expMs === null ? null : new Date(expMs).toISOString(),
      msUntilExpiry: expMs === null ? null : expMs - now,
      expired: expMs !== null && expMs <= now,
      fingerprint: `...${resolved.token.slice(-4)}`,
      clientId: this.clientId(),
    };
  }

  /** The credentials for an outbound call. Throws rather than sending junk. */
  require(): { token: string; clientId: string } {
    const resolved = this.resolve();
    if (!resolved) throw new NoTokenError();

    const expMs = decodeJwtExp(resolved.token);
    if (expMs !== null && expMs <= Date.now()) {
      throw new TokenExpiredError(new Date(expMs).toISOString());
    }

    const clientId = this.clientId();
    if (!clientId) {
      throw new ConfigError(
        'No Dhan client id. Set DHAN_CLIENT_ID, or pass client_id to set_dhan_token.',
      );
    }
    return { token: resolved.token, clientId };
  }
}
