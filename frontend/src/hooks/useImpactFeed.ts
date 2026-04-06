import { useState, useEffect, useRef } from 'react';
import { ImpactReport } from '@/types';
import { useWebSocket } from './useWebSocket';
import { api } from '@/lib/api';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/feed`;
const MAX_ITEMS = 100;

export function useImpactFeed() {
  const { status, lastMessage, send } = useWebSocket(WS_URL);
  const [reports, setReports] = useState<ImpactReport[]>([]);
  const hydrated = useRef(false);

  // Hydrate from REST on mount (survives page refresh)
  useEffect(() => {
    if (hydrated.current) return;
    hydrated.current = true;

    api.getImpactFeed(50, false)
      .then(({ items }) => {
        const valid = items.filter(
          (r): r is ImpactReport => r && typeof r.id === 'string' && r.news_item
        );
        if (valid.length > 0) {
          setReports(valid.slice(0, MAX_ITEMS));
        }
      })
      .catch((err) => {
        console.warn('Impact feed hydration failed:', err);
      });
  }, []);

  // Prepend live WebSocket events
  useEffect(() => {
    if (!lastMessage) return;

    if (lastMessage.type === 'impact_report') {
      const report = lastMessage.data as ImpactReport;
      if (!report?.id || !report?.news_item) return;
      setReports((prev) => {
        // Deduplicate by id
        const filtered = prev.filter((r) => r.id !== report.id);
        return [report, ...filtered].slice(0, MAX_ITEMS);
      });
    }
  }, [lastMessage]);

  return { reports, wsStatus: status, send };
}
