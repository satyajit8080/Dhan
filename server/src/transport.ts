/**
 * HTTP transport: one session, rate-limited, retried with jittered backoff.
 *
 * READ-ONLY BY CONSTRUCTION. `assertReadOnlyPath` rejects any path that looks
 * like an order, trade, or fund-movement endpoint before a request can leave
 * this process. A typo, a copy-paste, or a future edit cannot turn this client
 * into a trading client without deleting that guard on purpose.
 */

import {
  ApiError,
  AuthRejectedError,
  RateLimitError,
  TransportError,
  ValidationError,
} from './errors.js';
import { RateLimiter, backoffWithJitter, type LimitClass } from './ratelimit.js';
import type { Logger, Settings, TokenProvider } from './config.js';

/** Any path containing one of these is refused locally. */
const FORBIDDEN_PATH_FRAGMENTS = [
  'orders',
  'order',
  'trades',
  'super',
  'forever',
  'edis',
  'funds',
  'margin',
  'positions',
  'holdings',
  'alerts',
  'kill',
];

export function assertReadOnlyPath(path: string): void {
  const p = path.toLowerCase();
  for (const frag of FORBIDDEN_PATH_FRAGMENTS) {
    if (p.includes(frag)) {
      throw new ValidationError(
        `Refusing to call "${path}": this server is read-only market data. ` +
          `Order, trade, position and fund endpoints are not reachable from here.`,
        { path, matched: frag },
      );
    }
  }
}

/** Dhan codes that mean "you are throttled", across all their spellings. */
const THROTTLE_CODES = new Set(['RL001', 'DH-904', '904', 'DH_904']);
/** Dhan codes that mean "your credentials are bad". */
const AUTH_CODES = new Set(['DH-901', '901', '808', '809', '810', '811', 'DH-902', '902']);

export interface DhanEnvelope<T> {
  data?: T;
  status?: string;
  errorCode?: string;
  errorType?: string;
  errorMessage?: string;
  internalErrorCode?: string;
  internalErrorMessage?: string;
}

export interface RequestOptions {
  path: string;
  body: unknown;
  limitClass: LimitClass;
  uniqueKey?: string;
  maxAttempts?: number;
  timeoutMs?: number;
}

export class Transport {
  constructor(
    private readonly settings: Settings,
    private readonly tokens: TokenProvider,
    private readonly limiter: RateLimiter,
    private readonly log: Logger,
  ) {}

  get rateLimiter(): RateLimiter {
    return this.limiter;
  }

  /**
   * POST a Dhan v2 endpoint and unwrap the envelope.
   *
   * Returns both the payload and the epoch ms at which the response was
   * received — normalization stamps that as the leg's receipt time, which is
   * how the single-timestamp rule is enforced downstream.
   */
  async post<T>(opts: RequestOptions): Promise<{ data: T; receivedAtMs: number }> {
    assertReadOnlyPath(opts.path);

    const maxAttempts = opts.maxAttempts ?? 4;
    const timeoutMs = opts.timeoutMs ?? 15_000;
    const url = `${this.settings.baseUrl}${opts.path}`;

    let lastErr: unknown;

    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      await this.limiter.acquire(opts.limitClass, opts.uniqueKey);

      const { token, clientId } = this.tokens.require();
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);

      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json',
            'access-token': token,
            'client-id': clientId,
          },
          body: JSON.stringify(opts.body),
          signal: controller.signal,
        });
        const receivedAtMs = Date.now();
        clearTimeout(timer);

        const text = await res.text();
        let parsed: DhanEnvelope<T>;
        try {
          parsed = text ? (JSON.parse(text) as DhanEnvelope<T>) : {};
        } catch {
          throw new TransportError(`Non-JSON response from ${opts.path}`, {
            httpStatus: res.status,
            bodyPreview: text.slice(0, 200),
          });
        }

        const code = String(
          parsed.errorCode ?? parsed.internalErrorCode ?? '',
        ).toUpperCase();
        const msg =
          parsed.errorMessage ?? parsed.internalErrorMessage ?? `HTTP ${res.status}`;

        // --- throttled ----------------------------------------------------
        if (res.status === 429 || THROTTLE_CODES.has(code)) {
          const headerRetry = Number(res.headers.get('retry-after'));
          const waitMs = Number.isFinite(headerRetry) && headerRetry > 0
            ? headerRetry * 1000
            : backoffWithJitter(attempt);
          this.log.warn('Throttled by Dhan; backing off', {
            path: opts.path,
            code: code || res.status,
            attempt,
            waitMs: Math.round(waitMs),
          });
          lastErr = new RateLimitError(`Throttled on ${opts.path}: ${msg}`, waitMs, {
            code,
            httpStatus: res.status,
          });
          if (attempt < maxAttempts - 1) {
            await new Promise((r) => setTimeout(r, waitMs));
            continue;
          }
          throw lastErr;
        }

        // --- auth ----------------------------------------------------------
        if (res.status === 401 || res.status === 403 || AUTH_CODES.has(code)) {
          throw new AuthRejectedError(
            `Dhan rejected the credentials on ${opts.path}: ${msg}. ` +
              `The token may have expired — run /dhan-token with a fresh one.`,
            { code, httpStatus: res.status },
          );
        }

        // --- retryable server side ------------------------------------------
        if (res.status >= 500) {
          lastErr = new ApiError(`Dhan server error on ${opts.path}: ${msg}`, {
            httpStatus: res.status,
            code,
          }, true);
          if (attempt < maxAttempts - 1) {
            await new Promise((r) => setTimeout(r, backoffWithJitter(attempt)));
            continue;
          }
          throw lastErr;
        }

        if (!res.ok) {
          throw new ApiError(`Dhan error on ${opts.path}: ${msg}`, {
            httpStatus: res.status,
            code,
          });
        }

        if (parsed.status && parsed.status !== 'success') {
          throw new ApiError(`Dhan returned status="${parsed.status}" on ${opts.path}: ${msg}`, {
            code,
            status: parsed.status,
          });
        }

        if (parsed.data === undefined) {
          throw new ApiError(`Dhan response had no data block on ${opts.path}`, { code });
        }

        return { data: parsed.data, receivedAtMs };
      } catch (err) {
        clearTimeout(timer);

        if (err instanceof AuthRejectedError || err instanceof ApiError) {
          if (!(err as ApiError).retryable) throw err;
          lastErr = err;
        } else if (err instanceof RateLimitError) {
          lastErr = err;
        } else if (err instanceof Error && err.name === 'AbortError') {
          lastErr = new TransportError(`Timeout after ${timeoutMs}ms on ${opts.path}`, {
            path: opts.path,
          });
        } else if (err instanceof TransportError) {
          lastErr = err;
        } else {
          throw err;
        }

        if (attempt < maxAttempts - 1) {
          const wait = backoffWithJitter(attempt);
          this.log.warn('Request failed; retrying', {
            path: opts.path,
            attempt,
            waitMs: Math.round(wait),
          });
          await new Promise((r) => setTimeout(r, wait));
          continue;
        }
        throw lastErr;
      }
    }

    throw lastErr ?? new TransportError(`Exhausted retries on ${opts.path}`);
  }
}
