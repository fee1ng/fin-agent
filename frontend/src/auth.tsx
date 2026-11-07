import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { Alert, Button, Spin } from 'antd';
import { api, ApiError, setIdentity, type Identity } from './api';

interface Auth { user: Identity | null; login: (username: string, password: string) => Promise<void>; logout: () => Promise<void> }
const AuthContext = createContext<Auth | null>(null);
export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('Auth provider is required');
  return context;
}

export function AuthProvider({ children }: {children: ReactNode}) {
  const [user, update] = useState<Identity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const commit = (identity: Identity | null) => { setIdentity(identity); update(identity); };
  useEffect(() => {
    let alive = true;
    setLoading(true); setError(false);
    api.me().then(identity => { if (alive) commit(identity); }).catch(reason => {
      if (!alive) return;
      if (reason instanceof ApiError && reason.status === 401) commit(null);
      else setError(true);
    }).finally(() => { if (alive) setLoading(false); });
    const expire = () => { if (alive) commit(null); };
    window.addEventListener('fin:unauthorized', expire);
    return () => { alive = false; window.removeEventListener('fin:unauthorized', expire); };
  }, [attempt]);
  if (loading) return <div className="page-loading"><Spin size="large" description="正在连接工作台"><span /></Spin></div>;
  if (error) return <div className="page-loading"><Alert title="暂时无法连接服务" type="error" description="请检查网络后重试。"
    action={<Button onClick={() => setAttempt(a => a + 1)}>重新连接</Button>} /></div>;
  return <AuthContext.Provider value={{ user,
    login: async (username, password) => { commit(await api.login(username, password)); },
    logout: async () => { await api.logout(); commit(null); },
  }}>{children}</AuthContext.Provider>;
}
