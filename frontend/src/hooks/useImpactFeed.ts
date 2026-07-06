import { useState, useEffect, useRef, useCallback } from 'react';
import { ImpactReport } from '@/types';
import { useWebSocket } from './useWebSocket';
import { api } from '@/lib/api';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/feed`;
const HYDRATE_LIMIT = 100;
const PAGE_LIMIT = 100;

function isValid(r: any): r is ImpactReport {
  return r && typeof r.id === 'string' && r.news_item;
}

/** Merge two report lists, dedupe by id, sort newest-first. Uncapped: the
 *  list grows only as far as the user actually pages back; rows are cheap to
 *  hold and the DOM cost is contained via content-visibility on feed rows. */
function mergeReports(a: ImpactReport[], b: ImpactReport[]): ImpactReport[] {
  const byId = new Map<string, ImpactReport>();
  for (const r of a) byId.set(r.id, r);
  for (const r of b) byId.set(r.id, r); // b wins on collision (fresher payload)
  return [...byId.values()].sort((x, y) => (y.created_at ?? '').localeCompare(x.created_at ?? ''));
}

export function useImpactFeed() {
  const { status, lastMessage, send } = useWebSocket(WS_URL);
  const [reports, setReports] = useState<ImpactReport[]>([]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // hasMore stays true until an older page comes back short of a full page.
  const [hasMore, setHasMore] = useState(true);
  const prevStatus = useRef<typeof status | null>(null);

  // Hydrate from REST on mount AND on every reconnect. Events that arrived
  // during a disconnect are otherwise lost forever (WS only pushes live).
  // Merge+dedupe means re-hydration is safe to run repeatedly.
  useEffect(() => {
    const justConnected = status === 'connected' && prevStatus.current !== 'connected';
    prevStatus.current = status;
    if (!justConnected) return;

    // Hydrate relevant-only to match what the WS broadcasts (relevant events).
    api
      .getImpactFeed(HYDRATE_LIMIT, true)
      .then(({ items }) => {
        const valid = items.filter(isValid);
        if (valid.length > 0) {
          setReports((prev) => mergeReports(prev, valid));
        }
      })
      .catch((err) => {
        console.warn('Impact feed hydration failed:', err);
      });
  }, [status]);

  // Prepend live WebSocket events.
  useEffect(() => {
    if (!lastMessage) return;
    if (lastMessage.type === 'impact_report') {
      const report = lastMessage.data as ImpactReport;
      if (!isValid(report)) return;
      setReports((prev) => mergeReports(prev, [report]));
    }
  }, [lastMessage]);

  // Page back through older events using the created_at keyset cursor.
  const loadOlder = useCallback(async () => {
    if (loadingOlder || !hasMore) return;
    const oldest = reports[reports.length - 1]?.created_at;
    if (!oldest) return;
    setLoadingOlder(true);
    try {
      const { items } = await api.getImpactFeed(PAGE_LIMIT, true, oldest);
      const valid = items.filter(isValid);
      if (valid.length < PAGE_LIMIT) setHasMore(false);
      if (valid.length > 0) setReports((prev) => mergeReports(prev, valid));
    } catch (err) {
      console.warn('Impact feed pagination failed:', err);
    } finally {
      setLoadingOlder(false);
    }
  }, [reports, loadingOlder, hasMore]);

  return { reports, wsStatus: status, send, lastMessage, loadOlder, loadingOlder, hasMore };
}
