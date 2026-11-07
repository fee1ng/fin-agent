import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Input, Spin, Tag } from 'antd';
import { ArrowUpOutlined, ReloadOutlined } from '@ant-design/icons';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ApiError, request } from './api';
import { reduceEvent, terminal, type Run, type Snapshot, type StreamEvent } from './chat-state';

const labels: Record<string, string> = { queued: '排队中', running: '执行中', completed: '已完成', failed: '失败', interrupted: '中断' };
const toolLabel: Record<string, string> = {get_current_time: '确认数据时间', get_daily_price: '查询历史行情', get_realtime_price: '查询实时行情', run_backtest: '运行策略回测', get_stock_news_em: '检索股票新闻'};

export default function Chat({sessionId}: {sessionId: string}) {
  const [state, setState] = useState<Snapshot>({messages: [], runs: [], tools: []});
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [connection, setConnection] = useState('');
  const [executorAvailable, setExecutorAvailable] = useState(true);
  const [retry, setRetry] = useState(0);
  const attempt = useRef<{text: string; key: string} | null>(null);
  const retryAttempt = useRef<{runId: string; key: string} | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const active = state.runs.find(run => !terminal(run.status));

  useEffect(() => {
    let alive = true;
    setLoading(true); setReady(false); setError('');
    request<Snapshot>(`/sessions/${sessionId}/messages`).then(value => { if (alive) { setState(value); setReady(true); setExecutorAvailable(value.executor?.available ?? true); } })
      .catch(reason => { if (alive) {
        setError(reason instanceof Error ? reason.message : '会话加载失败');
        if (reason instanceof ApiError && reason.status === 404) setState({messages: [], runs: [], tools: []});
      } })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [sessionId, retry]);

  useEffect(() => {
    if (!active || loading || !ready) return;
    const source = new EventSource(`/api/v1/runs/${active.id}/events?after=${active.last_seq}`);
    source.onopen = () => setConnection('');
    source.onerror = () => setConnection('连接已断开，正在恢复；已提交的问题仍保存在服务端。');
    const receive = (raw: Event) => {
      try { const event = JSON.parse((raw as MessageEvent).data) as StreamEvent; setState(previous => reduceEvent(previous, event)); }
      catch { source.close(); setError('消息读取失败，请重新加载会话'); }
    };
    for (const type of ['run.queued', 'run.started', 'message.delta', 'message.interim', 'message.completed', 'tool.started', 'tool.finished', 'run.completed', 'run.failed', 'run.interrupted']) source.addEventListener(type, receive);
    source.addEventListener('stream.reset', () => { source.close(); setRetry(value => value + 1); });
    source.addEventListener('auth.expired', () => { source.close(); window.dispatchEvent(new Event('fin:unauthorized')); });
    source.addEventListener('executor.status', raw => {
      try { setExecutorAvailable(Boolean(JSON.parse((raw as MessageEvent).data).available)); } catch { /* next status refresh will retry */ }
    });
    return () => { source.close(); setConnection(''); };
    // last_seq advances within this one native EventSource; reconnect uses Last-Event-ID.
  }, [active?.id, loading, ready]);

  useEffect(() => { if (follow.current && feed.current) feed.current.scrollTop = feed.current.scrollHeight; }, [state.messages, state.tools]);

  const submit = async () => {
    const text = draft.trim();
    if (!text || sending || active || !ready) return;
    setSending(true); setError('');
    if (!attempt.current || attempt.current.text !== text) attempt.current = {text, key: crypto.randomUUID()};
    try {
      await request<Run>(`/sessions/${sessionId}/runs`, {method: 'POST',
        headers: {'Idempotency-Key': attempt.current.key}, body: JSON.stringify({text})});
      setDraft(''); attempt.current = null; follow.current = true;
      setLoading(true);
      setRetry(value => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '提交失败，请重试'); }
    finally { setSending(false); }
  };

  const retryFailed = async (runId: string) => {
    if (sending || active || loading || !ready) return;
    if (retryAttempt.current?.runId !== runId) retryAttempt.current = {runId, key: crypto.randomUUID()};
    setSending(true); setError('');
    try {
      await request<Run>(`/runs/${runId}/retry`, {method: 'POST', headers: {'Idempotency-Key': retryAttempt.current.key}});
      retryAttempt.current = null; setLoading(true); setRetry(value => value + 1); follow.current = true;
    } catch (reason) { setError(reason instanceof Error ? reason.message : '重试未完成，请稍后再试'); }
    finally { setSending(false); }
  };

  return <section className="chat-panel">
    {loading && <div className="list-hint"><Spin /></div>}
    {error && <Alert type="error" title={error} action={<Button size="small" icon={<ReloadOutlined />} onClick={() => setRetry(value => value + 1)}>重新加载</Button>} />}
    {connection && <Alert type="warning" title={connection} />}
    {active && !executorAvailable && <Alert type="warning" title="执行服务暂时不可用，请等待恢复后查看任务状态。" />}
    <div className="chat-feed" ref={feed} aria-label="聊天记录" onScroll={() => {
      if (feed.current) follow.current = feed.current.scrollHeight - feed.current.scrollTop - feed.current.clientHeight < 90;
    }}>
      {!state.messages.length && !loading && !error && <div className="chat-welcome"><span className="research-mark">↗</span><h1>你想研究什么？</h1><p>可以查询市场行情、结合新闻分析个股，或回测一个策略。</p></div>}
      {state.messages.map(message => <article className={`chat-message ${message.role} ${message.phase}`} key={message.id}>
        <div className="message-label">{message.role === 'user' ? '你' : message.phase === 'interim' ? '分析进展' : 'Fin-Agent'}
          {['failed', 'interrupted'].includes(message.status) && <Tag color="orange">{labels[message.status]}</Tag>}</div>
        <div className="message-content"><Markdown skipHtml remarkPlugins={[remarkGfm]} components={{img: () => null, a: ({children, ...props}) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>}}>{message.content}</Markdown></div>
      </article>)}
      {state.tools.length > 0 && <div className="tool-list" aria-label="工具执行状态">{state.tools.filter(tool => tool.run_id === (active?.id ?? state.runs.at(-1)?.id)).map(tool =>
        <Tag key={tool.span_id} color={tool.status === 'failed' ? 'orange' : 'default'} title={tool.name}>
          {toolLabel[tool.name] ?? '金融数据查询'} · {labels[tool.status] ?? tool.status}{tool.duration_ms != null ? ` · ${(tool.duration_ms / 1000).toFixed(2)}秒` : ''}
        </Tag>)}</div>}
      {active && <div className="run-progress"><Spin size="small" /> {labels[active.status]} · {executorAvailable ? '关闭页面后，任务仍会继续。' : '等待执行服务恢复。'}</div>}
      {!active && ['failed', 'interrupted'].includes(state.runs.at(-1)?.status ?? '') && <Alert type="warning" title="本次分析未完成，可主动重试。"
        description="重试会重新调用模型，可能再次产生调用费用。" action={<Button loading={sending} disabled={loading || !ready} onClick={() => void retryFailed(state.runs.at(-1)!.id)}>重试分析</Button>} />}
    </div>
    <div className="composer"><Input.TextArea aria-label="金融分析问题" placeholder="提一个市场问题，例如：分析平安银行最近一个月的走势" value={draft}
      autoSize={{minRows: 2, maxRows: 8}} maxLength={8000} disabled={!!active || loading || sending || !ready}
      onChange={event => setDraft(event.target.value)} onPressEnter={event => {
        if (!event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); }
      }} />
      <div className="composer-actions"><small>Enter 发送 · Shift + Enter 换行</small><Button aria-label="发送问题" type="primary" icon={<ArrowUpOutlined />} onClick={() => void submit()} loading={sending} disabled={!draft.trim() || !!active || loading || !ready}>发送</Button></div>
    </div>
  </section>;
}
