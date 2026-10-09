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

/*
 * Error-code families, per the DhanHQ v2 annexure (Trading API DH-9xx codes and
 * Data API 8xx codes). The Data API codes are the ones /charts, /marketfeed and
 * /optionchain actually return.
 */

/** "You are throttled": DH-904, and Data API 805 (too many requests). */
const THROTTLE_CODES = new Set(['RL001', 'DH-904', '904', 'DH_904', '805']);
/**
 * "Your credentials are bad": DH-901, 807 (token expired), 808 (auth failed),
 * 809 (token invalid), 810 (client id invalid).
 *
 * 811 is NOT here: it means "invalid expiry date", a request error.
 */
const AUTH_CODES = new Set(['DH-901', '901', '807', '808', '809', '810']);
/**
 * "Your account lacks the API access": DH-902, and Data API 806 (Data APIs not
 * subscribed). A fresh token does not fix these; activating the data plan does.
 */
const SUBSCRIPTION_CODES = new Set(['DH-902', '902', '806']);
/** Transient server-side failures worth a backoff retry: 800, DH-908, DH-909. */
const RETRYABLE_CODES = new Set(['800', 'DH-908', '908', 'DH-909', '909']);

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
        let res: Response;
        try {
          res = await fetch(url, {
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
        } catch (err) {
          // fetch() rejects with a TypeError ("fetch failed") on DNS failure,
          // connection reset or TLS error. Those are transient network faults
          // and must be retried like a timeout, not thrown on the first try.
          if (err instanceof Error && err.name === 'AbortError') throw err;
          throw new TransportError(`Network error on ${opts.path}: ${(err as Error).message}`, {
            path: opts.path,
            cause: String((err as { cause?: unknown }).cause ?? ''),
          });
        }
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

        // --- data plan / API access (checked before the HTTP-status auth rule,
        //     because Dhan can send 806/DH-902 with a 401/403) ----------------
        if (SUBSCRIPTION_CODES.has(code)) {
          throw new ApiError(
            `Dhan refused ${opts.path}: ${msg} (${code}). The account lacks the required ` +
              `API access — check the Data API plan under My Profile > Access DhanHQ APIs. ` +
              `A new token alone will not fix this.`,
            { code, httpStatus: res.status },
          );
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
        if (res.status >= 500 || RETRYABLE_CODES.has(code)) {
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
