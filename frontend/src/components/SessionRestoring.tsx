import { useEffect, useRef } from 'react';
import { useAuth } from '../auth/AuthProvider';

export function SessionRestoring() {
  const { restoreFailed, retryRestore, logout } = useAuth();
  const title = useRef<HTMLHeadingElement>(null);
  // R-6: when the restore fails, put focus on the screen's heading (unless the
  // visitor is already on one of its buttons, e.g. after a failed Retry).
  useEffect(() => {
    if (restoreFailed && !title.current?.parentElement?.contains(document.activeElement)) title.current?.focus({ preventScroll: true });
  }, [restoreFailed]);
  if (restoreFailed) {
    // D-RC-2: the server could not be reached. The saved session is kept; nothing
    // from a previous session is shown, and nothing redirects (no loop).
    return <section className="container state-panel" aria-labelledby="restore-failed-title">
      <h1 id="restore-failed-title" className="state-title" ref={title} tabIndex={-1}>Can’t reach RideShare</h1>
      <p role="alert">We couldn’t reach RideShare to restore your session. You’re still signed in on this tab. We’ll keep trying, or you can retry now.</p>
      <div className="state-actions">
        <button type="button" className="button" onClick={retryRestore}>Retry</button>
        <button type="button" className="button button-outline" onClick={logout}>Sign out</button>
      </div>
    </section>;
  }
  return <section className="container state-panel" role="status" aria-live="polite"><span className="spinner blue" /><p>Restoring your session…</p></section>;
}
