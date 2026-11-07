import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { Alert, Button, Card, Collapse, Descriptions, Empty, Form, Input, Select, Space, Spin, Statistic, Table, Tag, Typography } from 'antd';
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons';
import { request } from './api';
import Brand from './Brand';
import type { AdminDetail, AdminRun, Call } from './admin-types';

const labels: Record<string, string> = {queued: '排队中', running: '执行中', completed: '已完成', failed: '失败', interrupted: '中断', unknown: '未知'};
const formatTime = (seconds: number | null) => seconds == null ? '未知' : new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium', hour12: false}).format(new Date(seconds * 1000));
const duration = (ms: number | null | undefined) => ms == null ? '未知' : ms >= 1000 ? `${(ms / 1000).toFixed(2)} 秒` : `${ms.toFixed(1)} ms`;
const statusTag = (value: string) => <Tag color={value === 'completed' ? 'green' : ['failed', 'interrupted'].includes(value) ? 'orange' : 'default'}>{labels[value] ?? value}</Tag>;
const pretty = (value: unknown) => value == null ? '未记录' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);

interface Filters {user_id?: string; status?: string; tool?: string; session_id?: string; run_id?: string; since?: string; until?: string}

function RunList() {
  const [users, setUsers] = useState<{id: string; username: string; disabled: boolean}[]>([]);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [rows, setRows] = useState<AdminRun[]>([]);
  const [total, setTotal] = useState(0);
  const [form] = Form.useForm<Filters>();
  useEffect(() => {
    let alive = true;
    request<{users: typeof users}>('/admin/users').then(result => { if (alive) setUsers(result.users); }).catch(() => {});
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError('');
    request<{runs: AdminRun[]; total: number}>(`/admin/runs?${query}&offset=${(page - 1) * 25}&limit=25`, {signal: abort.signal})
      .then(result => { if (!abort.signal.aborted) { setRows(result.runs); setTotal(result.total); } })
      .catch(reason => { if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : '运行记录加载失败'); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [query, page, reload]);
  const apply = (values: Filters) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) if (value?.trim()) {
      params.set(key, ['since', 'until'].includes(key) ? new Date(value + '+08:00').toISOString() : value.trim());
    }
    setQuery(params.toString()); setPage(1);
  };
  return <><div className="admin-title"><div><span className="eyebrow">EXECUTION OBSERVABILITY</span><h1>运行记录</h1><p>从一次提问，追踪到每次模型与工具调用。</p></div><Button icon={<ReloadOutlined />} onClick={() => setReload(value => value + 1)}>刷新记录</Button></div>
    <Card className="admin-filters"><Form form={form} layout="vertical" onFinish={apply}>
      <div className="admin-filter-grid">
        <Form.Item name="user_id" label="用户"><Select aria-label="筛选用户" allowClear placeholder="全部用户" options={users.map(u => ({value: u.id, label: u.username + (u.disabled ? '（已禁用）' : '')}))} /></Form.Item>
        <Form.Item name="status" label="运行状态"><Select aria-label="筛选运行状态" allowClear placeholder="全部状态" options={Object.entries(labels).filter(([key]) => key !== 'unknown').map(([value, label]) => ({value, label}))} /></Form.Item>
        <Form.Item name="tool" label="工具名称"><Input placeholder="例如 get_daily_price" /></Form.Item>
        <Form.Item name="run_id" label="运行编号"><Input placeholder="精确匹配运行编号" /></Form.Item>
        <Form.Item name="session_id" label="会话编号"><Input placeholder="精确匹配会话编号" /></Form.Item>
        <Form.Item name="since" label="开始时间（北京时间）"><Input type="datetime-local" /></Form.Item>
        <Form.Item name="until" label="结束时间（北京时间）"><Input type="datetime-local" /></Form.Item>
      </div><Space><Button htmlType="submit" type="primary">应用筛选</Button><Button onClick={() => {form.resetFields(); setQuery(''); setPage(1);}}>重置</Button></Space>
    </Form></Card>
    {error && <Alert type="error" title={error} showIcon />}
    <Table<AdminRun> rowKey="id" dataSource={rows} loading={loading} scroll={{x: 1180}} pagination={{current: page, pageSize: 25, total, showSizeChanger: false, onChange: setPage, showTotal: count => `共 ${count} 次运行`}}
      columns={[
        {title: '提问与编号', key: 'input', width: 290, render: (_, row) => <div><Link to={`/admin/runs/${row.id}`}>{row.input_preview || '查看运行'}</Link><Typography.Text className="run-id" copyable>{row.id}</Typography.Text></div>},
        {title: '用户', dataIndex: 'username'}, {title: '状态', dataIndex: 'status', render: statusTag},
        {title: '提交时间（北京）', dataIndex: 'received_at', render: formatTime, width: 180},
        {title: '排队', dataIndex: 'queue_ms', render: duration}, {title: '执行', dataIndex: 'execution_ms', render: duration},
        {title: '首段正文', dataIndex: 'first_content_ms', render: duration},
        {title: '工具', render: (_, row) => <span>{row.tool_calls} 次{row.failed_tools > 0 && <Tag color="orange">{row.failed_tools} 失败/中断</Tag>}</span>},
      ]} />
  </>;
}

function RunDetail({id}: {id: string}) {
  const [detail, setDetail] = useState<AdminDetail | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError('');
    request<AdminDetail>(`/admin/runs/${encodeURIComponent(id)}`, {signal: abort.signal}).then(value => { if (!abort.signal.aborted) setDetail(value); })
      .catch(reason => { if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : '诊断详情加载失败'); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [id, reload]);
  if (loading) return <div className="page-loading"><Spin /></div>;
  if (error || !detail) return <Alert type="error" title={error || '运行不存在'} action={<Button onClick={() => setReload(value => value + 1)}>重试</Button>} />;
  const run = detail.run;
  const extent = Math.max(1, run.execution_ms ?? 0, ...detail.spans.map(span => (span.offset_ms ?? 0) + (span.duration_ms ?? 0)));
  return <><Link to="/admin/runs">← 返回运行记录</Link><div className="admin-title"><div><span className="eyebrow">RUN DIAGNOSTICS</span><h1>调用时间线 {statusTag(run.status)}</h1><Typography.Text copyable>{run.id}</Typography.Text></div><Button icon={<ReloadOutlined />} onClick={() => setReload(value => value + 1)}>刷新详情</Button></div>
    <Descriptions bordered size="small" column={{xs: 1, sm: 2, lg: 3}} items={[
      {key: 'user', label: '用户', children: run.username}, {key: 'session', label: '会话', children: run.title},
      {key: 'engine', label: '执行引擎', children: run.engine}, {key: 'received', label: '提交时间（北京）', children: formatTime(run.received_at)},
      {key: 'started', label: '执行开始', children: formatTime(run.started_at)}, {key: 'finished', label: '执行结束', children: formatTime(run.finished_at)},
    ]} />
    <div className="metric-grid">{[['排队时间', duration(run.queue_ms)], ['执行时间', duration(run.execution_ms)], ['首段正文（服务端）', duration(run.first_content_ms)], ['模型 / 工具调用', `${detail.model_calls} / ${detail.tool_calls}`]].map(([title, value]) => <Card key={title}><Statistic title={title} value={value} /></Card>)}</div>
    <Card title="问题与回答" className="admin-section"><p className="diagnostic-text">{detail.input.text}</p>{detail.answers.map(answer => <div key={answer.id} className="diagnostic-answer"><Tag>{answer.phase === 'interim' ? '中间进展' : '回答'}</Tag><pre>{answer.content}</pre></div>)}</Card>
    {detail.trace_state !== 'available' && <Alert type="info" title={detail.trace_state === 'expired' ? '详细追踪已过期；聊天及必要上下文仍保留。' : '本次运行暂无详细追踪，可能尚未执行。'} />}
    <div className="admin-section"><h2>模型与工具调用</h2><p className="muted">独立调用按实际开始时间排列；橙色标记失败或中断，最长调用单独标注。</p>
      <Table<Call> rowKey="id" dataSource={detail.spans} pagination={false} scroll={{x: 1000}} expandable={{expandedRowRender: span => <div className="span-details">
        <Descriptions size="small" column={2} items={[
          {key: 'id', label: '调用编号', children: span.id}, {key: 'call', label: '工具调用 ID', children: span.tool_call_id ?? '不适用'},
          {key: 'parent', label: '关联模型调用', children: span.parent_id ?? '未记录'}, {key: 'first', label: '模型首片段', children: `${duration(span.first_chunk_ms)}${span.first_chunk_kind ? `（${span.first_chunk_kind === 'tool_call' ? '工具参数' : '正文'}）` : ''}`},
          {key: 'usage', label: 'Token 用量', children: span.usage ? `输入 ${span.usage.input ?? '未知'} / 输出 ${span.usage.output ?? '未知'} / 总计 ${span.usage.total ?? '未知'}` : '未知'},
        ]} />
        <Collapse items={[
          {key: 'args', label: '脱敏参数', children: <pre>{pretty(span.arguments)}</pre>},
          {key: 'summary', label: '结果摘要', children: <pre>{pretty(span.summary)}</pre>},
          ...(span.error ? [{key: 'error', label: '错误详情', children: <pre>{pretty(span.error)}</pre>}] : []),
        ]} />
      </div>}} columns={[
        {title: '调用', width: 270, render: (_, span) => <div><Tag>{span.kind === 'model' ? '模型' : span.kind === 'tool' ? '工具' : '执行'}</Tag><strong>{span.name}</strong>{span.id === detail.slowest_span_id && <Tag color="blue">最长调用</Tag>}</div>},
        {title: '状态', dataIndex: 'status', render: statusTag},
        {title: '时间线', width: 240, render: (_, span) => <div className="span-bar-track" aria-label={`${span.name} 调用时间线`}><span className={span.status === 'completed' ? 'span-bar' : 'span-bar attention'} style={{left: `${Math.min(97, (span.offset_ms ?? 0) / extent * 100)}%`, width: `${Math.max(2, Math.min(100, (span.duration_ms ?? 0) / extent * 100))}%`}} /></div>},
        {title: '开始（北京）', dataIndex: 'started_at', render: formatTime}, {title: '耗时', dataIndex: 'duration_ms', render: duration},
        {title: '首片段', dataIndex: 'first_chunk_ms', render: duration},
      ]} locale={{emptyText: <Empty description="暂无调用记录" />}} />
    </div><Alert type="info" title="追踪范围" description={<><p>{detail.coverage}</p><p>整体 Token 用量：输入 {detail.usage.input ?? '未知'} / 输出 {detail.usage.output ?? '未知'} / 总计 {detail.usage.total ?? '未知'}。浏览器首帧耗时：未采集。</p></>} />
  </>;
}

export default function Admin() {
  const {runId} = useParams();
  const navigate = useNavigate();
  return <div className="admin-page"><header className="admin-header"><Brand /><Space><Tag color="green">管理员诊断</Tag><Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/chat')}>返回聊天</Button></Space></header>
    <main className="admin-content">{runId ? <RunDetail key={runId} id={runId} /> : <RunList />}</main><footer className="workspace-footer">诊断数据包含用户输入、回答及脱敏后的调用信息，仅限管理员查看。</footer></div>;
}
