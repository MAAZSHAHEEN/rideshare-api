import { useEffect } from 'react';

/**
 * Sets the document title and moves focus to the view heading when a driver view
 * opens. Driver views are separate paths, so the layout's own route effect (which
 * focuses <main> and sets a generic title) runs after ours in the same commit:
 * both steps wait one task so they land last.
 */
export function useDriverView(title: string, headingId: string) {
  useEffect(() => {
    const timer = setTimeout(() => {
      document.title = `${title} | RideShare`;
      document.getElementById(headingId)?.focus({ preventScroll: true });
    }, 0);
    return () => clearTimeout(timer);
  }, [title, headingId]);
}
