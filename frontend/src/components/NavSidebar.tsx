import { ReactNode, useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Logo, LiveDot } from './ds';
import { useAuth } from '@/auth/AuthContext';

const SIDEBAR_MIN = 168;
const SIDEBAR_MAX = 420;

// `feature` = admin-panel feature key (backend FEATURE_REGISTRY); items whose
// feature the admin disabled for this user are hidden. Server 403 is the
// real gate — this is cosmetics.
const NAV: { to: string; label: string; key: string; feature?: string }[] = [
  { to: '/terminal', label: 'Feed', key: 'feed' },
  { to: '/watchlist', label: 'Watchlist', key: 'watchlist', feature: 'watchlist' },
  { to: '/starred', label: 'Starred', key: 'starred' },
  { to: '/surprises', label: 'Surprises', key: 'surprises', feature: 'surprises' },
  { to: '/transcripts', label: 'Transcripts', key: 'transcripts', feature: 'transcripts' },
  { to: '/investors', label: 'Smart Money', key: 'investors', feature: 'investors' },
  { to: '/universe', label: 'Universe', key: 'universe' },
];

interface Props {
  /** Optional middle section (sectors, tracked symbols, coverage). */
  children?: ReactNode;
  /** Right-aligned counts per nav item. */
  counts?: Partial<Record<string, number | string>>;
  /** Pages with a live WS stream pass this; the live dot + label track it. */
  connected?: boolean;
  statusLabel?: string;
  /** Pages WITHOUT a live stream pass a truthful static label (no live dot). */
  staticStatus?: string;
}

export function NavSidebar({ children, counts, connected = false, statusLabel = 'LIVE FEED', staticStatus }: Props) {

  const { pathname } = useLocation();
  const { user, logout } = useAuth();
  const disabled = new Set(user?.disabled_features ?? []);
  const isAdmin = user?.role === 'admin';
  let navItems: typeof NAV = user?.internal
    ? [
        ...NAV,
        { to: '/research', label: 'Research', key: 'research', feature: 'research_chat' },
        { to: '/papertrading', label: 'Paper Trading', key: 'papertrading', feature: 'papertrading' },
      ]
    : NAV;
  // Admin sees everything regardless of overrides (mirrors the backend gate).
  if (!isAdmin) navItems = navItems.filter((i) => !i.feature || !disabled.has(i.feature));
  if (isAdmin) navItems = [...navItems, { to: '/admin', label: 'Admin', key: 'admin' }];

  const monogram = (user?.name || user?.email || 'AM')
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0]?.toUpperCase())
    .join('');

  // Restore persisted sidebar width on mount (drives the pages' grid column via --sidebar-w).
  useEffect(() => {
    const saved = localStorage.getItem('tr_sidebar_w');
    if (saved) document.documentElement.style.setProperty('--sidebar-w', `${saved}px`);
  }, []);

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const onMove = (ev: MouseEvent) => {
      const w = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, Math.round(ev.clientX)));
      document.documentElement.style.setProperty('--sidebar-w', `${w}px`);
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      const cur = getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w').trim();
      if (cur) localStorage.setItem('tr_sidebar_w', String(parseInt(cur, 10)));
    };
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  return (
    <div
      style={{
        position: 'relative',
        width: '100%',
        flexShrink: 0,
        background: 'var(--bg-elev)',
        borderRight: '1px solid var(--border)',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
      }}
    >
      {/* Drag handle on the right edge to resize the sidebar */}
      <div
        onMouseDown={startResize}
        title="Drag to resize"
        style={{ position: 'absolute', top: 0, right: -3, width: 6, height: '100%', cursor: 'col-resize', zIndex: 10 }}
      />
      <div style={{ padding: '16px 16px', borderBottom: '1px solid var(--border)' }}>
        <Logo size={20} showTag={false} />
      </div>

      <nav style={{ padding: '12px 10px', display: 'flex', flexDirection: 'column', gap: 3 }}>
        {navItems.map((item) => {
          const active = pathname === item.to;
          return (
            <Link
              key={item.key}
              to={item.to}
              className="mono"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 9,
                fontSize: 11.5,
                padding: '9px 12px',
                borderRadius: 4,
                textDecoration: 'none',
                borderLeft: `2px solid ${active ? 'var(--accent)' : 'transparent'}`,
                background: active ? 'var(--bg-elev-2)' : 'transparent',
                color: active ? 'var(--ink)' : 'var(--ink-dim)',
              }}
            >
              <span
                style={{
                  width: 5,
                  height: 5,
                  borderRadius: 1,
                  background: active ? 'var(--accent)' : 'var(--ink-faint)',
                  flexShrink: 0,
                }}
              />
              {item.label}
              {counts?.[item.key] !== undefined && (
                <span className="num" style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--ink-mute)' }}>
                  {counts[item.key]}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {children && <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '4px 10px' }}>{children}</div>}
      {!children && <div style={{ flex: 1 }} />}

      <div style={{ borderTop: '1px solid var(--border)', padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {staticStatus ? (
            // Stream-less page: neutral dot, truthful static label — no fake "connected".
            <>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--ink-faint)' }} />
              <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)' }}>
                {staticStatus}
              </span>
            </>
          ) : (
            <>
              {connected ? (
                <LiveDot color="tail" />
              ) : (
                <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--ink-faint)' }} />
              )}
              <span className="mono upper" style={{ fontSize: 10, letterSpacing: '0.14em', color: 'var(--ink-dim)' }}>
                {connected ? statusLabel : 'OFFLINE'}
              </span>
            </>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
          {user?.picture ? (
            <img src={user.picture} alt="" style={{ width: 27, height: 27, borderRadius: '50%' }} />
          ) : (
            <span
              className="mono"
              style={{
                width: 27,
                height: 27,
                borderRadius: '50%',
                background: 'var(--accent-dim)',
                color: 'var(--accent)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 10,
                flexShrink: 0,
              }}
            >
              {monogram || 'AM'}
            </span>
          )}
          <div style={{ minWidth: 0, flex: 1 }}>
            <div className="mono" style={{ fontSize: 11.5, color: 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {user?.name || user?.email || 'Analyst'}
            </div>
            {user?.name && user?.email && (
              <div className="mono" style={{ fontSize: 10, color: 'var(--ink-mute)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {user.email}
              </div>
            )}
          </div>
          <button
            onClick={() => logout()}
            title="Sign out"
            className="mono upper"
            style={{
              background: 'transparent',
              border: '1px solid var(--border-hi)',
              color: 'var(--ink-mute)',
              borderRadius: 4,
              fontSize: 10,
              letterSpacing: '0.1em',
              padding: '4px 6px',
              cursor: 'pointer',
            }}
          >
            Exit
          </button>
        </div>
      </div>
    </div>
  );
}
