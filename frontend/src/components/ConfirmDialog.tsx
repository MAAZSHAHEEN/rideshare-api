import { useEffect, useId, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';

/**
 * Small accessible modal: labelled, focus starts on the safe choice, Tab stays
 * inside, Escape cancels, and focus returns to the element that opened it.
 */
export function ConfirmDialog({ title, children, confirmLabel, cancelLabel, busy, error, onConfirm, onCancel }: {
  title: string; children: ReactNode; confirmLabel: string; cancelLabel: string; busy?: boolean; error?: string;
  onConfirm: () => void; onCancel: () => void;
}) {
  const titleId = useId();
  const bodyId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const safe = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    safe.current?.focus();
    return () => { if (opener && document.contains(opener)) opener.focus(); };
  }, []);
  function keyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape' && !busy) { event.preventDefault(); onCancel(); return; }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]') ?? []);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  return <div className="dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onCancel(); }}>
    <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={bodyId} ref={dialog} onKeyDown={keyDown}>
      <h2 id={titleId}>{title}</h2>
      <div id={bodyId} className="dialog-body">{children}</div>
      {error && <div className="notice error" role="alert">{error}</div>}
      <div className="dialog-actions">
        <button ref={safe} className="button button-outline" type="button" onClick={onCancel} disabled={busy}>{cancelLabel}</button>
        <button className="button danger" type="button" onClick={onConfirm} disabled={busy}>{busy ? <><span className="spinner" aria-hidden="true" />Working…</> : confirmLabel}</button>
      </div>
    </div>
  </div>;
}
