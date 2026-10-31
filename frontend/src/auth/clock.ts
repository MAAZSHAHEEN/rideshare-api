// Clocks for the session-expiry hint (D-RC-1).
// - wallNow: the device's wall clock. Only used for elapsed time across a reload
//   (performance.now() restarts per document); never compared with token claims.
// - monoNow: monotonic time within this document. Immune to the device clock being
//   set forward or back while the page is open, so the in-page expiry timer uses it.
export const sessionClock = {
  wallNow: (): number => Date.now(),
  monoNow: (): number => (typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : Date.now()),
};
