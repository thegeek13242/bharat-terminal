/**
 * /admin — founder control panel.
 *
 * Tabs: Users (invite/roles/features) · Analytics (per-user engagement) ·
 * Health (containers + loop liveness + kafka lag) · Latency · Costs ·
 * Alerts (history) · Paper (ledger reset) · Audit (admin action log).
 *
 * Client-side gating is cosmetic (NavSidebar shows the item for role=admin);
 * the real gate is require_admin on every /admin/* endpoint — a 403 anywhere
 * flips the whole page to the denied panel and stops polling (PaperTrading
 * denied-latch pattern).
 */
import { useState } from 'react';
import { NavSidebar } from '@/components/NavSidebar';
import { useAuth } from '@/auth/AuthContext';
import { DeniedPanel } from './shared';
import { UsersTab } from './UsersTab';
import { AnalyticsTab } from './AnalyticsTab';
import { HealthTab } from './HealthTab';
import { LatencyTab } from './LatencyTab';
import { CostsTab } from './CostsTab';
import { AlertsTab } from './AlertsTab';
import { PaperTab } from './PaperTab';
import { AuditTab } from './AuditTab';

const TABS = [
  { key: 'users', label: 'Users' },
  { key: 'analytics', label: 'Analytics' },
  { key: 'health', label: 'Health' },
  { key: 'latency', label: 'Latency' },
  { key: 'costs', label: 'Costs' },
  { key: 'alerts', label: 'Alerts' },
  { key: 'paper', label: 'Paper' },
  { key: 'audit', label: 'Audit' },
] as const;

type TabKey = (typeof TABS)[number]['key'];

export function AdminPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<TabKey>('users');
  const [denied, setDenied] = useState(false);

  const onDenied = () => setDenied(true);

  return (
    <div style={{ height: '100vh', display: 'grid', gridTemplateColumns: 'var(--sidebar-w, 184px) 1fr', overflow: 'hidden' }}>
      <NavSidebar staticStatus="ADMIN PANEL" />
      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden' }}>
        <div style={{ padding: '14px 20px 0', borderBottom: '1px solid var(--border)' }}>
          <div className="mono upper" style={{ fontSize: 12, letterSpacing: '0.16em', color: 'var(--ink)' }}>
            Admin
            <span className="mono" style={{ marginLeft: 10, fontSize: 10, letterSpacing: 0, textTransform: 'none', color: 'var(--ink-mute)' }}>
              {user?.email}
            </span>
          </div>
          <div style={{ display: 'flex', gap: 2, marginTop: 12, overflowX: 'auto' }}>
            {TABS.map((t) => {
              const active = tab === t.key;
              return (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className="mono upper"
                  style={{
                    background: 'transparent',
                    border: 'none',
                    borderBottom: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                    color: active ? 'var(--ink)' : 'var(--ink-dim)',
                    fontSize: 10.5,
                    letterSpacing: '0.1em',
                    padding: '8px 12px',
                    cursor: 'pointer',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {t.label}
                </button>
              );
            })}
          </div>
        </div>

        {denied ? (
          <DeniedPanel />
        ) : (
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '16px 20px' }}>
            {tab === 'users' && <UsersTab onDenied={onDenied} />}
            {tab === 'analytics' && <AnalyticsTab onDenied={onDenied} />}
            {tab === 'health' && <HealthTab onDenied={onDenied} />}
            {tab === 'latency' && <LatencyTab onDenied={onDenied} />}
            {tab === 'costs' && <CostsTab onDenied={onDenied} />}
            {tab === 'alerts' && <AlertsTab onDenied={onDenied} />}
            {tab === 'paper' && <PaperTab onDenied={onDenied} />}
            {tab === 'audit' && <AuditTab onDenied={onDenied} />}
          </div>
        )}
      </div>
    </div>
  );
}

export interface TabProps {
  onDenied: () => void;
}
