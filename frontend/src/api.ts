export interface Identity { id: string; username: string; role: 'user' | 'admin'; csrf_token: string }
export interface ChatSession { id: string; title: string; created_at: number; updated_at: number }

let csrf = '';
export function setIdentity(identity: Identity | null) { csrf = identity?.csrf_token ?? ''; }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET' && csrf) headers.set('X-CSRF-Token', csrf);
  const response = await fetch(`/api/v1${path}`, { ...options, credentials: 'same-origin', headers });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const message = typeof body?.detail === 'string' ? body.detail : '请求未完成，请稍后重试';
    if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('fin:unauthorized'));
    throw new ApiError(response.status, message);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export const api = {
  me: () => request<Identity>('/auth/me'),
  login: (username: string, password: string) => request<Identity>('/auth/login', {
    method: 'POST', body: JSON.stringify({ username, password }),
  }),
  logout: () => request<void>('/auth/logout', { method: 'POST' }),
  sessions: (offset = 0) => request<{sessions: ChatSession[]}>(`/sessions?offset=${offset}&limit=50`),
  create: (title = '新会话') => request<ChatSession>('/sessions', { method: 'POST', body: JSON.stringify({ title }) }),
  rename: (id: string, title: string) => request<ChatSession>(`/sessions/${encodeURIComponent(id)}`, {
    method: 'PATCH', body: JSON.stringify({ title }),
  }),
  remove: (id: string) => request<void>(`/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
};
