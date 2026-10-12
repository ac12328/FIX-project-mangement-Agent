"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  ApiError,
  type AuthSession,
  getAuthSession,
  logout,
  logoutAll,
} from "@/lib/api";

type AuthContextValue = {
  session: AuthSession | null;
  loading: boolean;
  error: string | null;
  refreshSession: () => Promise<AuthSession | null>;
  hasPermission: (permission: string) => boolean;
  signOut: () => Promise<void>;
  signOutAll: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refreshSession = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await getAuthSession();
      setSession(next);
      return next;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setSession(null);
        return null;
      }
      const message = err instanceof Error ? err.message : "Could not verify your session.";
      setError(message);
      setSession(null);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshSession().catch(() => undefined);
  }, [refreshSession]);

  const hasPermission = useCallback(
    (permission: string) => Boolean(session?.permissions?.includes(permission)),
    [session],
  );

  const signOut = useCallback(async () => {
    try {
      await logout();
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 401) throw err;
    } finally {
      setSession(null);
    }
  }, []);

  const signOutAll = useCallback(async () => {
    try {
      await logoutAll();
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 401) throw err;
    } finally {
      setSession(null);
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ session, loading, error, refreshSession, hasPermission, signOut, signOutAll }),
    [session, loading, error, refreshSession, hasPermission, signOut, signOutAll],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider");
  return context;
}
