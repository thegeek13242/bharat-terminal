import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ImpactReport } from '@/types';
import { api } from '@/lib/api';

/**
 * Account-level starred events, backed by GET/PUT/DELETE /starred.
 * Toggles are optimistic and roll back (with an error message) on failure,
 * so a dead API never silently loses or fabricates a star.
 */
export function useStarred() {
  const [reports, setReports] = useState<ImpactReport[]>([]);
  const [error, setError] = useState<string | null>(null);
  const reportsRef = useRef<ImpactReport[]>([]);
  useEffect(() => {
    reportsRef.current = reports;
  }, [reports]);

  useEffect(() => {
    let cancelled = false;
    api
      .getStarred()
      .then(({ items }) => {
        if (!cancelled) setReports((items as ImpactReport[]).filter((r) => r?.id));
      })
      .catch(() => {
        if (!cancelled) setError('Could not load starred events.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const isStarred = useCallback(
    (id: string) => reports.some((r) => r.id === id),
    [reports],
  );

  const toggleStar = useCallback((r: ImpactReport) => {
    const prev = reportsRef.current;
    const on = prev.some((x) => x.id === r.id);
    setReports(on ? prev.filter((x) => x.id !== r.id) : [r, ...prev]);
    setError(null);
    (on ? api.unstarEvent(r.id) : api.starEvent(r.id)).catch(() => {
      setReports(prev); // roll back the optimistic toggle
      setError('Could not save star — reverted.');
    });
  }, []);

  // Newest-first by event time so FeedList's day dividers stay coherent.
  const starred = useMemo(
    () =>
      [...reports].sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? '')),
    [reports],
  );

  return { starred, isStarred, toggleStar, count: reports.length, error };
}
