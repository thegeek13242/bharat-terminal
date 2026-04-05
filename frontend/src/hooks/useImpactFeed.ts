import { useState, useEffect } from 'react';
import { ImpactReport } from '@/types';
import { useWebSocket } from './useWebSocket';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/feed`;
const MAX_ITEMS = 100;

export function useImpactFeed() {
  const { status, lastMessage, send } = useWebSocket(WS_URL);
  const [reports, setReports] = useState<ImpactReport[]>([]);

  useEffect(() => {
    if (!lastMessage) return;

    if (lastMessage.type === 'impact_report') {
      const report = lastMessage.data as ImpactReport;
      setReports((prev) => {
        const updated = [report, ...prev];
        return updated.slice(0, MAX_ITEMS);
      });
    }
  }, [lastMessage]);

  return { reports, wsStatus: status, send };
}
