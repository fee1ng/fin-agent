import { lazy, Suspense, useEffect, useState } from 'react';
import { Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom';
import { Alert, App as AntApp, Button, Form, Input, Modal, Popconfirm, Spin, Tag, Tooltip } from 'antd';
import { DeleteOutlined, EditOutlined, LogoutOutlined, MessageOutlined, PlusOutlined } from '@ant-design/icons';
import { useAuth } from './auth';
import { api, type ChatSession } from './api';
import Brand from './Brand';
const Chat = lazy(() => import('./Chat'));
const Admin = lazy(() => import('./Admin'));


function Login() {
  const { user, login } = useAuth();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  if (user) return <Navigate to="/chat" replace />;
  const submit = async (values: {username: string; password: string}) => {
    setPending(true); setError('');
    try { await login(values.username, values.password); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '登录未完成'); }
    finally { setPending(false); }
  };
  return <main className="login-page">
    <section className="login-story"><Brand /><div className="story-copy"><span className="eyebrow">RESEARCH, WITH CONTEXT</span>
      <h1>从一个问题，<br />开始更有依据的分析。</h1><p>连接市场数据与金融工具，<br />让每一次研究都有迹可循。</p></div>
      <div className="story-foot"><span className="status-dot" /> 受控访问 · 只读分析</div></section>
    <section className="login-form"><div className="login-card"><span className="eyebrow">WELCOME BACK</span><h2>登录工作台</h2>
      <p className="muted">使用管理员为你创建的账号继续。</p>
      {error && <Alert role="alert" title={error} type="error" showIcon className="form-error" />}
      <Form layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item label="账号" name="username" rules={[{ required: true, message: '请输入账号' }]}>
          <Input autoComplete="username" placeholder="你的账号" size="large" maxLength={64} /></Form.Item>
        <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}>
          <Input.Password autoComplete="current-password" placeholder="输入密码" size="large" maxLength={256} /></Form.Item>
        <Button htmlType="submit" type="primary" size="large" loading={pending} block>登录</Button>
      </Form><p className="login-note">账号由管理员分配。如需开通或恢复访问，请联系管理员。</p>
    </div><footer>Fin-Agent · 以数据支持判断</footer></section>
  </main>;
}

function Workspace() {
  const { user, logout } = useAuth();
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const { sessionId } = useParams();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<ChatSession | null>(null);
  const [form] = Form.useForm<{title: string}>();
  const selected = sessions.find(s => s.id === sessionId);
  const load = async (more = false) => {
    setLoading(true); setFailed(false);
    try {
      const result = await api.sessions(more ? sessions.length : 0);
      setSessions(previous => more ? [...previous, ...result.sessions] : result.sessions);
      setHasMore(result.sessions.length === 50);
    } catch { setFailed(true); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);
  const create = async () => {
    setBusy(true);
    try { const session = await api.create(); setSessions(items => [session, ...items]); navigate(`/chat/${session.id}`); }
    catch (reason) { void message.error(reason instanceof Error ? reason.message : '创建失败'); }
    finally { setBusy(false); }
  };
  const rename = async () => {
    const { title } = await form.validateFields();
    if (!editing) return;
    setBusy(true);
    try { const session = await api.rename(editing.id, title); setSessions(items => items.map(s => s.id === session.id ? session : s)); setEditing(null); }
    catch (reason) { void message.error(reason instanceof Error ? reason.message : '重命名失败'); }
    finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    try { await api.remove(id); setSessions(items => items.filter(s => s.id !== id)); if (id === sessionId) navigate('/chat'); }
    catch (reason) { void message.error(reason instanceof Error ? reason.message : '删除失败'); }
  };
  return <div className="workspace"><aside className="sidebar"><Brand />
    <Button type="primary" icon={<PlusOutlined />} onClick={create} loading={busy} block size="large">新建会话</Button>
    <div className="section-label">我的研究会话</div>
    {user?.role === 'admin' && <Button type="text" onClick={() => navigate('/admin/runs')}>运行记录与诊断</Button>}
    <nav aria-label="研究会话" className="session-list">
      {failed && <Alert type="error" title="会话加载失败" action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
      {sessions.map(session => <div key={session.id} className={`session-row ${session.id === sessionId ? 'selected' : ''}`}>
        <button className="session-select" onClick={() => navigate(`/chat/${session.id}`)} title={session.title}>
          <MessageOutlined /><span>{session.title}</span></button>
        <div className="session-actions"><Tooltip title="重命名"><Button type="text" size="small" aria-label={`重命名 ${session.title}`} icon={<EditOutlined />}
          onClick={() => { setEditing(session); form.setFieldsValue({title: session.title}); }} /></Tooltip>
          <Popconfirm title="删除这个会话？" description="聊天及关联诊断数据将一并删除。" onConfirm={() => remove(session.id)} okText="删除" cancelText="保留">
            <Button type="text" size="small" aria-label={`删除 ${session.title}`} icon={<DeleteOutlined />} /></Popconfirm></div>
      </div>)}
      {loading && <div className="list-hint"><Spin size="small" /></div>}
      {!loading && !failed && !sessions.length && <p className="list-hint">还没有会话，从一个问题开始。</p>}
      {hasMore && <Button block type="text" disabled={loading} onClick={() => void load(true)}>加载更多</Button>}
    </nav>
    <div className="account"><span className="avatar">{user?.username[0].toUpperCase()}</span><div><strong>{user?.username}</strong><small>{user?.role === 'admin' ? '管理员' : '研究账号'}</small></div>
      <Tooltip title="退出登录"><Button type="text" aria-label="退出登录" icon={<LogoutOutlined />} onClick={() => { void logout().catch(() => message.error('退出未完成，请重试')); }} /></Tooltip></div>
  </aside><main className="main-panel"><header className="workspace-header"><div><span className="eyebrow">RESEARCH SPACE</span><h2>{selected?.title ?? '研究工作台'}</h2></div><Tag color="green">只读分析</Tag></header>
    {sessionId ? <Chat key={sessionId} sessionId={sessionId} /> : <section className="research-empty"><div className="research-mark">↗</div><span className="eyebrow">ASK. EXPLORE. UNDERSTAND.</span>
      <h1>{selected ? '新的研究，新的视角。' : '把市场问题，逐步理清。'}</h1>
      <p>{selected ? '这个会话将保存你的问题与分析结果。' : '新建一个会话，为每个研究主题留出独立空间。'}</p>
      {!selected && <Button onClick={create} loading={busy} icon={<PlusOutlined />} size="large">开始新的研究</Button>}
      <div className="capability-grid"><div><span>01 / 市场</span><h3>行情与趋势</h3><p>从公开行情数据建立分析依据</p></div><div><span>02 / 研究</span><h3>新闻与指标</h3><p>结合市场动态，理解变化背景</p></div><div><span>03 / 策略</span><h3>选股与回测</h3><p>用历史数据检验策略假设</p></div></div>
    </section>}<footer className="workspace-footer">分析供研究参考，数据时效以查询结果为准。管理员可查看会话及脱敏诊断数据。</footer></main>
    <Modal title="重命名会话" open={!!editing} confirmLoading={busy} onOk={() => void rename()} onCancel={() => setEditing(null)} okText="保存" cancelText="取消" destroyOnHidden>
      <Form form={form} layout="vertical"><Form.Item label="会话名称" name="title" rules={[{ required: true, whitespace: true, message: '请输入会话名称' }]}>
        <Input maxLength={120} autoFocus onPressEnter={() => void rename()} /></Form.Item></Form>
    </Modal></div>;
}

export default function App() {
  const { user } = useAuth();
  return <Suspense fallback={<div className="list-hint"><Spin /> 正在加载工作区</div>}><Routes><Route path="/login" element={<Login />} />
    <Route path="/chat/:sessionId?" element={user ? <Workspace key={user.id} /> : <Navigate to="/login" replace />} />
    <Route path="/admin/runs/:runId?" element={user?.role === 'admin' ? <Admin /> : <Navigate to={user ? '/chat' : '/login'} replace />} />
    <Route path="*" element={<Navigate to={user ? '/chat' : '/login'} replace />} /></Routes></Suspense>;
}
