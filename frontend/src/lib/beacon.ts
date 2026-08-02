/**
 * Activity beacon — page views + visibility heartbeats.
 *
 * Powers /admin analytics (visits, time-on-platform, per-page dwell):
 * - page_view on every SPA route change (useBeacon effect on location)
 * - heartbeat every 60s, ONLY while the tab is visible; an immediate beat
 *   on return-to-visible so short glances register.
 *
 * All failures are swallowed — analytics must never affect the app.
 * keepalive lets a final POST survive tab close.
 */
import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';

const HEARTBEAT_MS = 60_000;

function send(type: 'page_view' | 'heartbeat', path: string) {
  try {
    void fetch('/api/events/beacon', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      keepalive: true,
      body: JSON.stringify({ type, path }),
    }).catch(() => {});
  } catch {
    /* never surface */
  }
}

/** Mount once inside the authenticated tree (App.tsx). */
export function useBeacon(enabled: boolean) {
  const { pathname } = useLocation();

  useEffect(() => {
    if (!enabled) return;
    send('page_view', pathname);
  }, [enabled, pathname]);

  useEffect(() => {
    if (!enabled) return;
    const beat = () => {
      if (document.visibilityState === 'visible') {
        send('heartbeat', window.location.pathname);
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') beat();
    };
    const timer = window.setInterval(beat, HEARTBEAT_MS);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [enabled]);
}
