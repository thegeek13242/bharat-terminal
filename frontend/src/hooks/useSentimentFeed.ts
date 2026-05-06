import { useEffect, useRef, useState } from 'react';
import { SentimentSnapshot, ThemeAggregate } from '@/types';
import { useWebSocket } from './useWebSocket';
import { api } from '@/lib/api';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/feed`;
const MAX_EVENTS = 200;
const THEME_REFRESH_MS = 30_000;

export function useSentimentFeed() {
  const { status, lastMessage } = useWebSocket(WS_URL);
  const [snapshots, setSnapshots] = useState<SentimentSnapshot[]>([]);
  const [themes, setThemes] = useState<ThemeAggregate[]>([]);
  const hydrated = useRef(false);

  // Hydrate event stream + theme aggregates on mount
  useEffect(() => {
    if (hydrated.current) return;
    hydrated.current = true;

    api
      .getSentimentRecent(MAX_EVENTS, 720)
      .then(({ items }) => {
        const valid = (items || []).filter(
          (s): s is SentimentSnapshot => !!s && typeof s.id === 'string',
        );
        setSnapshots(valid.slice(0, MAX_EVENTS));
      })
      .catch((err) => console.warn('Sentiment hydration failed:', err));

    const refreshThemes = () => {
      api
        .getSentimentThemes(720)
        .then(({ themes: t }) => setThemes(t || []))
        .catch((err) => console.warn('Theme hydration failed:', err));
    };
    refreshThemes();
    const id = window.setInterval(refreshThemes, THEME_REFRESH_MS);
    return () => window.clearInterval(id);
  }, []);

  // Prepend live WS events
  useEffect(() => {
    if (!lastMessage) return;
    if (lastMessage.type !== 'sentiment_snapshot') return;
    const snap = lastMessage.data as SentimentSnapshot;
    if (!snap?.id) return;
    setSnapshots((prev) => {
      const filtered = prev.filter((s) => s.id !== snap.id);
      return [snap, ...filtered].slice(0, MAX_EVENTS);
    });
  }, [lastMessage]);

  return { snapshots, themes, wsStatus: status };
}
