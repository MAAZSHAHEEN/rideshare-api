import { useCallback, useRef } from 'react';
import { ApiError } from './client';

/** A random UUID v4 for Idempotency-Key. crypto.randomUUID needs a secure context; fall back to getRandomValues. */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * True when the outcome of a request is unknown or transient, so pressing the
 * button again is a retry of the same action and must reuse its key: network
 * failure or timeout (0), 429, and 5xx. Any other answer settles the action.
 */
export function isRetryableFailure(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 0 || error.status === 429 || error.status >= 500;
}

/**
 * One Idempotency-Key per user action, keyed by an action scope such as
 * `book:7` or `respond:70:accept`. `keyFor` returns the pending key for that
 * scope or mints a new one; `settle` forgets it after success or a definitive
 * error, so the next press is a new action with a new key. Accept and reject on
 * the same request use different scopes (different requests must not share a key).
 */
export function useIdempotencyKeys() {
  const keys = useRef(new Map<string, string>());
  const keyFor = useCallback((scope: string) => {
    let key = keys.current.get(scope);
    if (!key) { key = newIdempotencyKey(); keys.current.set(scope, key); }
    return key;
  }, []);
  const settle = useCallback((scope: string, error?: unknown) => {
    if (error === undefined || !isRetryableFailure(error)) keys.current.delete(scope);
  }, []);
  return { keyFor, settle };
}
