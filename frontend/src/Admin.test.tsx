import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { App as AntApp } from 'antd';
import Admin from './Admin';
import App from './App';
import { AuthProvider } from './auth';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const row = {id: 'r1', user_id: 'u1', username: 'alice', session_id: 's1', title: '市场分析', status: 'completed',
  received_at: 1000, started_at: 1002, finished_at: 1005, engine: 'langgraph', queue_ms: 2000, execution_ms: 3000,
  total_ms: 5000, first_content_ms: 4000, input_preview: '分析平安银行', tool_calls: 2, failed_tools: 1, slowest_tool_ms: 1000};
const response = (value: unknown) => new Response(JSON.stringify(value), {headers: {'Content-Type': 'application/json'}});
const renderAdmin = (path: string) => render(<AntApp><MemoryRouter initialEntries={[path]}><Routes><Route path="/admin/runs/:runId?" element={<Admin />} /></Routes></MemoryRouter></AntApp>);

it('filters records with Beijing time conversion and navigates pages', async () => {
  const urls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/users')) return response({users: [{id: 'u1', username: 'alice', disabled: false}]});
    urls.push(url); return response({runs: [row], total: 26});
  }));
  const user = userEvent.setup(); renderAdmin('/admin/runs');
  await screen.findByRole('link', {name: '分析平安银行'});
  await user.click(screen.getByTitle('2'));
  await waitFor(() => expect(urls.at(-1)).toContain('offset=25'));
  await user.type(screen.getByLabelText('工具名称'), 'get_daily_price');
  fireEvent.change(screen.getByLabelText('开始时间（北京时间）'), {target: {value: '2026-11-05T12:30'}});
  await user.click(screen.getByRole('button', {name: '应用筛选'}));
  await waitFor(() => {
    const params = new URLSearchParams(urls.at(-1)!.split('?')[1]);
    expect(params.get('tool')).toBe('get_daily_price');
    expect(params.get('since')).toBe('2026-11-05T04:30:00.000Z');
    expect(params.get('offset')).toBe('0');
  });
});

it('shows per-call identity, redacted arguments, zero usage and an unknown aggregate', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({run: row, input: {message_id: 'q', text: '分析平安银行'},
    answers: [{id: 'a', content: '分析结果', phase: 'final', status: 'completed'}],
    spans: [
      {id: 'm1', kind: 'model', name: 'test-model', parent_id: null, tool_call_id: null, started_at: 1002,
       ended_at: 1003, offset_ms: 0, duration_ms: 1000, status: 'completed', first_chunk_ms: 200,
       first_chunk_kind: 'tool_call', usage: {input: 10, output: 0, total: 10}, arguments: null, summary: null, error: null},
      {id: 't1', kind: 'tool', name: 'get_daily_price', parent_id: 'm1', tool_call_id: 'call-1', started_at: 1003,
       ended_at: 1004, offset_ms: 1000, duration_ms: 1200, status: 'failed', first_chunk_ms: null, first_chunk_kind: null,
       usage: null, arguments: {api_key: '[REDACTED]'}, summary: 'Error: timeout', error: '行情源超时'},
    ], usage: {input: null, output: null, total: null}, model_calls: 1, tool_calls: 1, failed_tools: 1,
    trace_state: 'available', slowest_span_id: 't1', coverage: '记录应用层实际调用边界', browser_first_render_ms: null})));
  const user = userEvent.setup(); renderAdmin('/admin/runs/r1');
  await screen.findByText('最长调用');
  await user.click(screen.getAllByRole('button', {name: 'Expand row'})[0]);
  await screen.findByText('输入 10 / 输出 0 / 总计 10');
  await user.click(screen.getAllByRole('button', {name: 'Expand row'})[0]);
  await screen.findByText('call-1');
  await user.click(screen.getAllByText('脱敏参数').at(-1)!);
  await screen.findByText(/\[REDACTED\]/);
  await user.click(screen.getByText('错误详情'));
  await screen.findByText('行情源超时');
  expect(screen.getByText(/整体 Token 用量：输入 未知/)).toBeTruthy();
});

it('does not fetch or expose admin data when a normal account visits an admin URL', async () => {
  let adminCalls = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.includes('/admin/')) adminCalls += 1;
    return url.endsWith('/auth/me') ? response({id: 'normal', username: 'alice', role: 'user', csrf_token: 'csrf'}) : response({sessions: []});
  }));
  render(<AntApp><MemoryRouter initialEntries={['/admin/runs/r1']}><AuthProvider><App /></AuthProvider></MemoryRouter></AntApp>);
  await screen.findByRole('button', {name: /新建会话/});
  expect(adminCalls).toBe(0);
  expect(screen.queryByText('运行记录与诊断')).toBeNull();
});

it('explains expired traces while keeping the conversation and aggregate call counts', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({run: row, input: {message_id: 'q', text: '历史问题'},
    answers: [{id: 'a', content: '保留的回答', phase: 'final', status: 'completed'}], spans: [],
    usage: {input: null, output: null, total: null}, model_calls: 2, tool_calls: 3, failed_tools: 0,
    trace_state: 'expired', slowest_span_id: null, coverage: '应用层实际调用', browser_first_render_ms: null})));
  renderAdmin('/admin/runs/r1');
  await screen.findByText('详细追踪已过期；聊天及必要上下文仍保留。');
  expect(screen.getByText('保留的回答')).toBeTruthy();
  expect(screen.getByText('2 / 3')).toBeTruthy();
});
