import { createContext, useContext, useEffect, useState, ReactNode, useCallback } from 'react';
import { api, setUnauthorizedHandler } from '@/lib/api';
import { AuthUser } from '@/types';

interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  /** Exchange a Google ID token for a session. Throws ApiError (403 = not invited). */
  loginWithGoogle: (idToken: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  const clear = useCallback(() => setUser(null), []);

  // On 401 from any API call, drop the user (ProtectedRoute then redirects).
  useEffect(() => {
    setUnauthorizedHandler(clear);
  }, [clear]);

  // Bootstrap session on load.
  useEffect(() => {
    api
      .me()
      .then(({ user }) => setUser(user))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const loginWithGoogle = useCallback(async (idToken: string) => {
    const { user } = await api.authGoogle(idToken);
    setUser(user);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setUser(null);
    }
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, loginWithGoogle, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
