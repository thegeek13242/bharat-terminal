import { useEffect, useRef, useState, useCallback } from 'react';

type WSStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

// Cloudflare closes idle WebSockets after ~100s. Send an application-level
// keepalive well inside that window; the server answers {"action":"ping"} with
// {"type":"pong"}.
const KEEPALIVE_MS = 30_000;

export function useWebSocket(url: string) {
  const wsRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<WSStatus>('disconnected');
  const [lastMessage, setLastMessage] = useState<any>(null);
  const reconnectDelay = useRef(1000);
  const mountedRef = useRef(true);
  const keepaliveRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const clearKeepalive = useCallback(() => {
    if (keepaliveRef.current) {
      clearInterval(keepaliveRef.current);
      keepaliveRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    setStatus('connecting');
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      setStatus('connected');
      reconnectDelay.current = 1000; // Reset backoff on success

      // Start (or restart) the keepalive ping.
      clearKeepalive();
      keepaliveRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ action: 'ping' }));
        }
      }, KEEPALIVE_MS);
    };

    ws.onmessage = (event) => {
      if (!mountedRef.current) return;
      try {
        const data = JSON.parse(event.data);
        // Swallow pong keepalive replies — they are not feed events.
        if (data?.type === 'pong') return;
        setLastMessage(data);
      } catch (e) {
        console.error('WS parse error:', e);
      }
    };

    ws.onclose = () => {
      clearKeepalive();
      if (!mountedRef.current) return;
      setStatus('disconnected');
      // Exponential backoff: 1s, 2s, 4s, 8s, max 30s
      const delay = Math.min(reconnectDelay.current, 30000);
      reconnectDelay.current = Math.min(delay * 2, 30000);
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      setStatus('error');
    };
  }, [url, clearKeepalive]);

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      clearKeepalive();
      wsRef.current?.close();
    };
  }, [connect, clearKeepalive]);

  const send = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  return { status, lastMessage, send };
}
