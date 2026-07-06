import { useEffect } from 'react';
import { ImpactReport } from '@/types';

interface Args {
  reports: ImpactReport[];
  selectedId: string | null;
  onSelect: (r: ImpactReport) => void;
  onEscape: () => void;
}

function isTypingTarget(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null;
  if (!node) return false;
  const tag = node.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || node.isContentEditable;
}

/**
 * Keyboard navigation for a feed list.
 *   ↑ / k  → previous (newer) event
 *   ↓ / j  → next (older) event
 *   Enter  → no-op (selection already drives the panel)
 *   Esc    → clear explicit selection (return to auto-follow)
 * Keystrokes are ignored while focus is in a text input.
 */
export function useFeedKeyboardNav({ reports, selectedId, onSelect, onEscape }: Args) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target)) return;

      if (e.key === 'Escape') {
        onEscape();
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        return; // no-op: selection already drives the panel
      }

      const dir = e.key === 'ArrowDown' || e.key === 'j' ? 1 : e.key === 'ArrowUp' || e.key === 'k' ? -1 : 0;
      if (dir === 0 || reports.length === 0) return;
      e.preventDefault();

      const idx = reports.findIndex((r) => r.id === selectedId);
      const next = idx === -1 ? 0 : Math.min(reports.length - 1, Math.max(0, idx + dir));
      const target = reports[next];
      if (target) onSelect(target);
    };

    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [reports, selectedId, onSelect, onEscape]);
}
