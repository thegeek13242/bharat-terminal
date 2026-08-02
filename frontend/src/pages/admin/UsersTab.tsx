/** Users: invite, activate/deactivate, role changes, per-user feature toggles. */
import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import { relTime } from '@/lib/format';
import {
  apiFetch, Btn, ErrorNote, GridHeader, Hint, Input, SectionTitle, Select,
  StateChip,
} from './shared';
import type { TabProps } from './AdminPage';

interface AdminUser {
  id: number;
  email: string;
  name: string | null;
  role: 'admin' | 'internal' | 'external';
  is_active: boolean;
  invited_at: string | null;
  last_login_at: string | null;
  disabled_features: string[];
}

const GRID = 'minmax(220px,2fr) 90px 80px 110px 110px minmax(160px,1fr) 190px';
const ROLES = ['admin', 'internal', 'external'] as const;

export function UsersTab({ onDenied }: TabProps) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [registry, setRegistry] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<string>('external');
  const [inviteNote, setInviteNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [u, f] = await Promise.all([
        apiFetch<{ users: AdminUser[] }>('/admin/users'),
        apiFetch<{ registry: Record<string, string[]> }>('/admin/features'),
      ]);
      setUsers(u.users);
      setRegistry(f.registry);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    }
  }, [onDenied]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(key);
    try {
      await fn();
      await load();
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) return onDenied();
      setError(String((e as Error).message));
    } finally {
      setBusy(null);
    }
  };

  const invite = () =>
    act('invite', async () => {
      const res = await apiFetch<{ created: boolean; email_sent: boolean; email_error: string | null }>(
        '/admin/users',
        { method: 'POST', body: JSON.stringify({ email: inviteEmail, role: inviteRole }) },
      );
      setInviteNote(
        res.created
          ? `Invited. Email ${res.email_sent ? 'sent' : `NOT sent (${res.email_error ?? 'disabled'})`}.`
          : 'Account already exists — nothing changed (use the row actions to reactivate).',
      );
      setInviteEmail('');
    });

  const patch = (u: AdminUser, body: object) =>
    act(`patch-${u.id}`, () =>
      apiFetch(`/admin/users/${u.id}`, { method: 'PATCH', body: JSON.stringify(body) }));

  const setFeature = (u: AdminUser, feature: string, enabled: boolean) =>
    act(`feat-${u.id}-${feature}`, () =>
      apiFetch(`/admin/users/${u.id}/features`, {
        method: 'PUT',
        body: JSON.stringify({ feature, enabled }),
      }));

  return (
    <div>
      <SectionTitle first>Invite user</SectionTitle>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <Input
          value={inviteEmail}
          onChange={setInviteEmail}
          placeholder="email@firm.com"
          width={260}
          onKeyDown={(e) => { if (e.key === 'Enter' && inviteEmail) void invite(); }}
        />
        <Select value={inviteRole} onChange={setInviteRole} options={ROLES} width={110} />
        <Btn variant="primary" disabled={!inviteEmail || busy === 'invite'} onClick={() => void invite()}>
          {busy === 'invite' ? 'Inviting…' : 'Invite'}
        </Btn>
        {inviteNote && <Hint>{inviteNote}</Hint>}
      </div>

      <ErrorNote error={error} />

      <SectionTitle>Accounts ({users.length})</SectionTitle>
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <GridHeader grid={GRID} cols={['Email', 'Role', 'Active', 'Last login', 'Invited', 'Disabled features', 'Actions']} />
        {users.map((u) => (
          <div key={u.id}>
            <div
              className="row-hover"
              onClick={() => setExpanded(expanded === u.id ? null : u.id)}
              style={{
                display: 'grid', gridTemplateColumns: GRID, gap: 10, padding: '9px 12px',
                fontSize: 11.5, alignItems: 'center', cursor: 'pointer',
                borderBottom: '1px solid var(--border)',
                opacity: u.is_active ? 1 : 0.55,
              }}
            >
              <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{u.email}</span>
              <span>
                <StateChip ok={u.role === 'admin'} warn={u.role === 'internal'} label={u.role} />
              </span>
              <span className="mono" style={{ color: u.is_active ? 'var(--tail)' : 'var(--head)' }}>
                {u.is_active ? 'yes' : 'no'}
              </span>
              <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10.5 }}>{relTime(u.last_login_at)}</span>
              <span className="mono" style={{ color: 'var(--ink-mute)', fontSize: 10.5 }}>{relTime(u.invited_at)}</span>
              <span className="mono" style={{ fontSize: 10, color: u.disabled_features.length ? 'var(--warn)' : 'var(--ink-faint)' }}>
                {u.disabled_features.length ? u.disabled_features.join(', ') : 'none'}
              </span>
              <span style={{ display: 'flex', gap: 6, alignItems: 'center' }} onClick={(e) => e.stopPropagation()}>
                <Btn
                  small
                  variant={u.is_active ? 'danger' : 'ghost'}
                  disabled={!!busy}
                  onClick={() => {
                    const verb = u.is_active ? 'Deactivate' : 'Reactivate';
                    if (window.confirm(`${verb} ${u.email}?`)) void patch(u, { is_active: !u.is_active });
                  }}
                >
                  {u.is_active ? 'Deactivate' : 'Activate'}
                </Btn>
                <Select
                  small
                  width={92}
                  value={u.role}
                  disabled={!!busy}
                  options={ROLES}
                  onChange={(role) => {
                    if (window.confirm(`Set role of ${u.email} to ${role}?`)) {
                      void patch(u, { role });
                    }
                  }}
                />
              </span>
            </div>

            {expanded === u.id && (
              <div style={{ padding: '12px 12px 16px 24px', borderBottom: '1px solid var(--border)', background: 'var(--bg-elev-2)' }}>
                <div className="mono upper" style={{ fontSize: 9, letterSpacing: '0.12em', color: 'var(--ink-dim)', marginBottom: 10 }}>
                  Feature access {u.role === 'admin' && '(admin bypasses all gates)'}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 8 }}>
                  {Object.keys(registry).sort().map((feature) => {
                    const disabled = u.disabled_features.includes(feature);
                    const k = `feat-${u.id}-${feature}`;
                    return (
                      <label key={feature} className="mono" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, cursor: 'pointer', color: disabled ? 'var(--warn)' : 'var(--ink-2)' }}>
                        <input
                          type="checkbox"
                          checked={!disabled}
                          disabled={busy === k}
                          onChange={() => void setFeature(u, feature, disabled)}
                          style={{ accentColor: 'var(--accent)', width: 13, height: 13, margin: 0 }}
                        />
                        {feature}
                      </label>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
