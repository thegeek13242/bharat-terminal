import { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { GoogleLogin } from '@react-oauth/google';
import { useAuth } from '@/auth/AuthContext';
import { ApiError } from '@/lib/api';
import { Logo } from '@/components/ds';

const FEATURES = [
  'Live event feed · sub-second ingestion',
  'Pre-computed dossiers · 460+ covered names',
  'Exposure mapping · cross-sector propagation',
];

export function Login() {
  const { user, loading, loginWithGoogle } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!loading && user) return <Navigate to="/terminal" replace />;

  async function handleCredential(idToken: string) {
    setError(null);
    setBusy(true);
    try {
      await loginWithGoogle(idToken);
      navigate('/terminal', { replace: true });
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        setError('Access is invite-only. This Google account is not on the alpha allowlist — contact your desk admin.');
      } else if (e instanceof ApiError && e.status === 401) {
        setError('Could not verify your Google sign-in. Please try again.');
      } else {
        setError('Sign-in failed. Please try again.');
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="bg-grid" />
      <div style={{ position: 'fixed', inset: 0, display: 'grid', gridTemplateColumns: '1.05fr 0.95fr', fontFamily: 'var(--sans)', color: 'var(--ink)' }}>
        {/* Left: brand / positioning */}
        <div
          style={{
            background: 'var(--bg-elev)',
            borderRight: '1px solid var(--border)',
            padding: '52px 56px',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            gap: 56,
            minWidth: 0,
          }}
        >
          <Logo size={24} href="https://taperead.trade" />
          <div style={{ maxWidth: 440 }}>
            <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.2em', color: 'var(--accent)', marginBottom: 22 }}>
              Market Intelligence
            </div>
            <h1 style={{ fontFamily: 'var(--sans)', fontWeight: 600, fontSize: 38, lineHeight: 1.08, letterSpacing: '-0.02em', margin: '0 0 18px' }}>
              Read the tape before the desk does.
            </h1>
            <p style={{ fontSize: 14.5, lineHeight: 1.6, color: 'var(--ink-2)', margin: '0 0 36px' }}>
              Structured, real-time assessment of every market-moving event across the NSE universe — pre-computed
              dossiers, exposure maps, and channel-attributed impact analysis for institutional equity desks.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              {FEATURES.map((f) => (
                <div key={f} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ width: 6, height: 6, borderRadius: 1, background: 'var(--accent)', flexShrink: 0 }} />
                  <span className="mono" style={{ fontSize: 12, letterSpacing: '0.03em', color: 'var(--ink-2)' }}>{f}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Right: sign-in */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 40, minWidth: 0 }}>
          <div style={{ width: '100%', maxWidth: 352 }}>
            <div className="mono upper" style={{ fontSize: 10, letterSpacing: '0.2em', color: 'var(--ink-mute)', marginBottom: 14 }}>
              Sign in
            </div>
            <h2 style={{ fontFamily: 'var(--sans)', fontWeight: 600, fontSize: 24, letterSpacing: '-0.01em', margin: '0 0 8px' }}>
              Sign in to your desk
            </h2>
            <p style={{ fontSize: 13, lineHeight: 1.55, color: 'var(--ink-dim)', margin: '0 0 30px' }}>
              Authorized institutional users only.
            </p>

            {error && (
              <div
                style={{
                  background: 'rgba(198,132,118,0.1)',
                  border: '1px solid var(--head)',
                  borderRadius: 6,
                  padding: '11px 13px',
                  fontSize: 12,
                  lineHeight: 1.5,
                  color: 'var(--ink-2)',
                  marginBottom: 20,
                }}
              >
                {error}
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'center', minHeight: 44, opacity: busy ? 0.6 : 1, pointerEvents: busy ? 'none' : 'auto' }}>
              <GoogleLogin
                theme="filled_black"
                shape="rectangular"
                text="signin_with"
                width="352"
                onSuccess={(cred) => cred.credential && handleCredential(cred.credential)}
                onError={() => setError('Google sign-in was cancelled or failed.')}
              />
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 24, marginTop: 28, borderTop: '1px solid var(--border)' }}>
              <span style={{ width: 5, height: 5, borderRadius: 1, background: 'var(--ink-faint)', flexShrink: 0 }} />
              <span className="mono" style={{ fontSize: 10, letterSpacing: '0.04em', color: 'var(--ink-mute)', lineHeight: 1.5 }}>
                Sessions are logged for compliance.
              </span>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
