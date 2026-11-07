import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import Chat from './Chat';
import { setIdentity } from './api';

class TestStream extends EventTarget {
  static instances: TestStream[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { super(); TestStream.instances.push(this); }
  close() { this.closed = true; }
  emit(seq: number, type: string, data: Record<string, unknown>) {
    this.dispatchEvent(new MessageEvent(type, {data: JSON.stringify({v: 1, run_id: 'run-1', seq, type, data})}));
  }
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); TestStream.instances = []; setIdentity(null); });

it('submits once, disables concurrent input and consumes replayable Markdown/table events', async () => {
  vi.stubGlobal('EventSource', TestStream);
  setIdentity({id: 'u', username: 'alice', role: 'user', csrf_token: 'csrf'});
  let submitted = false;
  const posts: RequestInit[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    if (options.method === 'POST') {
      submitted = true; posts.push(options);
      return new Response(JSON.stringify({id: 'run-1'}), {status: 202});
    }
    return new Response(JSON.stringify({messages: submitted ? [{id: 'question', run_id: 'run-1', role: 'user', phase: 'final', status: 'completed', content: '查询行情'}] : [],
      tools: [], runs: submitted ? [{id: 'run-1', session_id: 's', status: 'queued', last_seq: 1}] : []}));
  }));
  const user = userEvent.setup();
  render(<Chat sessionId="s" />);
  const input = await screen.findByRole('textbox', {name: '金融分析问题'});
  await waitFor(() => expect(input.hasAttribute('disabled')).toBe(false));
  await user.type(input, '查询行情');
  await user.click(screen.getByRole('button', {name: '发送问题'}));
  await waitFor(() => expect(TestStream.instances.length).toBe(1));
  expect(input.hasAttribute('disabled')).toBe(true);
  expect(posts.length).toBe(1);
  expect(new Headers(posts[0].headers).get('Idempotency-Key')).toBeTruthy();
  expect(new Headers(posts[0].headers).get('X-CSRF-Token')).toBe('csrf');
  const stream = TestStream.instances[0];
  const text = '| 指标 | 数值 |\n| --- | --- |\n| 收盘价 | 10 |';
  act(() => {
    stream.emit(2, 'message.delta', {message_id: 'answer', text: '初稿'});
    stream.emit(2, 'message.delta', {message_id: 'answer', text: '初稿'});
    stream.emit(3, 'message.completed', {message_id: 'answer', text});
    stream.emit(4, 'run.completed', {});
  });
  await screen.findByRole('table');
  expect(screen.queryByText('初稿')).toBeNull();
  await waitFor(() => expect(stream.closed).toBe(true));
  expect(input.hasAttribute('disabled')).toBe(false);
});

it('retries an interrupted run with one stable idempotency key and no duplicate question', async () => {
  vi.stubGlobal('EventSource', TestStream);
  let accepted = false;
  const keys: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit) => {
    if (options.method === 'POST') {
      expect(url).toBe('/api/v1/runs/run-1/retry');
      keys.push(new Headers(options.headers).get('Idempotency-Key')!);
      if (keys.length === 1) throw new TypeError('暂时离线');
      accepted = true;
      return new Response(JSON.stringify({id: 'run-2'}), {status: 202});
    }
    return new Response(JSON.stringify({messages: [
      {id: 'q', run_id: 'run-1', role: 'user', phase: 'final', status: 'completed', content: '查询行情'},
      {id: 'm', run_id: 'run-1', role: 'assistant', phase: 'final', status: 'interrupted', content: '中断前的部分结果'},
    ], tools: [], runs: [{id: 'run-1', session_id: 's', status: 'interrupted', last_seq: 4},
      ...(accepted ? [{id: 'run-2', session_id: 's', status: 'queued', last_seq: 1, retry_of: 'run-1'}] : [])]}));
  }));
  const user = userEvent.setup();
  render(<Chat sessionId="s" />);
  await user.click(await screen.findByRole('button', {name: '重试分析'}));
  await screen.findByText('暂时离线');
  await user.click(screen.getByRole('button', {name: '重试分析'}));
  await waitFor(() => expect(TestStream.instances.length).toBe(1));
  expect(keys.length).toBe(2);
  expect(keys[0]).toBe(keys[1]);
  expect(TestStream.instances[0].url).toContain('/runs/run-2/events');
  expect(screen.getAllByText('你', {exact: true}).length).toBe(1);
  expect(screen.queryByRole('button', {name: '重试分析'})).toBeNull();
});

it('reconnects through an expired cursor snapshot without resubmitting a question', async () => {
  vi.stubGlobal('EventSource', TestStream);
  let reads = 0;
  let posts = 0;
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    if (options.method === 'POST') posts += 1;
    reads += 1;
    const completed = reads > 1;
    return new Response(JSON.stringify({messages: [{id: 'm', run_id: 'run-1', role: 'assistant', phase: 'final',
      status: completed ? 'completed' : 'draft', content: completed ? '恢复后的完整答案' : '旧片段'}], tools: [],
      runs: [{id: 'run-1', session_id: 's', status: completed ? 'completed' : 'running', last_seq: completed ? 7 : 3}],
      executor: {available: completed, heartbeat_at: null}}));
  }));
  render(<Chat sessionId="s" />);
  await screen.findByText('执行服务暂时不可用，请等待恢复后查看任务状态。');
  await waitFor(() => expect(TestStream.instances.length).toBe(1));
  const stream = TestStream.instances[0];
  act(() => { stream.onerror?.(); });
  await screen.findByText(/连接已断开，正在恢复/);
  act(() => { stream.dispatchEvent(new MessageEvent('stream.reset', {data: '{}'})); });
  await screen.findByText('恢复后的完整答案');
  expect(screen.queryByText('旧片段')).toBeNull();
  expect(posts).toBe(0);
  expect(reads).toBe(2);
  expect(stream.closed).toBe(true);
});

it('stops subscribing when another tab deletes a completed conversation before reconnect', async () => {
  vi.stubGlobal('EventSource', TestStream);
  let reads = 0;
  vi.stubGlobal('fetch', vi.fn(async () => {
    reads += 1;
    if (reads > 1) return new Response(JSON.stringify({detail: '会话不存在'}), {status: 404});
    return new Response(JSON.stringify({messages: [], tools: [], runs: [
      {id: 'run-1', session_id: 's', status: 'running', last_seq: 2},
    ]}));
  }));
  render(<Chat sessionId="s" />);
  await waitFor(() => expect(TestStream.instances.length).toBe(1));
  act(() => TestStream.instances[0].dispatchEvent(new MessageEvent('stream.reset', {data: '{}'})));
  await screen.findByText('会话不存在');
  expect(TestStream.instances[0].closed).toBe(true);
  expect(TestStream.instances.length).toBe(1);
  expect(screen.getByRole('textbox', {name: '金融分析问题'}).hasAttribute('disabled')).toBe(true);
});
