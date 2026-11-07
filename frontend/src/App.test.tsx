import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { App as AntApp } from 'antd';
import App from './App';
import { AuthProvider } from './auth';
import { setIdentity, type ChatSession, type Identity } from './api';

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', { writable: true, value: () => ({
    matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }) });
  const computed = window.getComputedStyle;
  window.getComputedStyle = element => computed(element);
});
beforeEach(() => {
  setIdentity(null);
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const identity: Identity = { id: 'user-1', username: 'alice', role: 'user', csrf_token: 'server-issued-csrf' };
function page() {
  return render(<AntApp><MemoryRouter initialEntries={['/chat']}><AuthProvider><App /></AuthProvider></MemoryRouter></AntApp>);
}
function json(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }); }

describe('authenticated session workflow', () => {
  it('logs in, creates, renames, deletes and logs out with CSRF on mutations', async () => {
    let loggedIn = false;
    let sessions: ChatSession[] = [];
    const mutationHeaders: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit) => {
      const path = url.split('?')[0];
      if (path.endsWith('/auth/me')) return loggedIn ? json(identity) : json({ detail: '请重新登录' }, 401);
      if (path.endsWith('/auth/login')) { loggedIn = true; return json(identity); }
      if (path.endsWith('/messages')) return json({messages: [], runs: [], tools: []});
      if (options.method && options.method !== 'GET') mutationHeaders.push(new Headers(options.headers).get('X-CSRF-Token') ?? '');
      if (path.endsWith('/auth/logout')) { loggedIn = false; return new Response(null, {status: 204}); }
      if (path.endsWith('/sessions') && options.method === 'POST') {
        const session = { id: 'session-1', title: '新会话', created_at: 1, updated_at: 1 };
        sessions = [session]; return json(session, 201);
      }
      if (options.method === 'PATCH') {
        sessions[0] = { ...sessions[0], title: JSON.parse(String(options.body)).title };
        return json(sessions[0]);
      }
      if (options.method === 'DELETE') { sessions = []; return new Response(null, {status: 204}); }
      return json({ sessions });
    }));
    const user = userEvent.setup();
    page();
    await user.type(await screen.findByLabelText('账号'), 'alice');
    await user.type(screen.getByLabelText('密码'), 'test-password');
    await user.click(screen.getByRole('button', {name: '登 录'}));
    await user.click(await screen.findByRole('button', {name: /新建会话/}));
    await screen.findByRole('heading', {name: '新会话'});
    await user.click(screen.getByRole('button', {name: '重命名 新会话'}));
    const input = await screen.findByLabelText('会话名称');
    await user.clear(input); await user.type(input, '银行业研究');
    await user.click(screen.getByRole('button', {name: '保 存'}));
    await screen.findByRole('heading', {name: '银行业研究'});
    await user.click(screen.getByRole('button', {name: '删除 银行业研究'}));
    await user.click(await screen.findByRole('button', {name: '删 除'}));
    await waitFor(() => expect(screen.queryByRole('heading', {name: '银行业研究'})).toBeNull());
    await user.click(screen.getByRole('button', {name: '退出登录'}));
    await screen.findByRole('heading', {name: '登录工作台'});
    expect(mutationHeaders.length).toBe(4);
    expect(mutationHeaders.every(value => value === identity.csrf_token)).toBe(true);
  });

  it('shows a login error without navigating to a workspace', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => json({detail: url.endsWith('/auth/login') ? '账号或密码错误' : '请重新登录'}, 401)));
    const user = userEvent.setup(); page();
    await user.type(await screen.findByLabelText('账号'), 'alice');
    await user.type(screen.getByLabelText('密码'), 'wrong');
    await user.click(screen.getByRole('button', {name: '登 录'}));
    await screen.findByText('账号或密码错误');
    expect(screen.queryByRole('button', {name: /新建会话/})).toBeNull();
  });

  it('keeps network failure distinct from logged out and supports retry', async () => {
    let online = false;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (!online) throw new TypeError('offline');
      return url.endsWith('/auth/me') ? json(identity) : json({sessions: []});
    }));
    const user = userEvent.setup(); page();
    await screen.findByText('暂时无法连接服务');
    expect(screen.queryByLabelText('账号')).toBeNull();
    online = true;
    await user.click(screen.getByRole('button', {name: '重新连接'}));
    await screen.findByRole('button', {name: /新建会话/});
    window.dispatchEvent(new Event('fin:unauthorized'));
    await screen.findByRole('heading', {name: '登录工作台'});
  });
});
