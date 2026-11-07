export interface Run { id: string; session_id: string; status: string; last_seq: number; retry_of?: string | null; input_message_id?: string | null }
export interface Message { id: string; run_id: string; role: string; phase: string; content: string; status: string }
export interface Tool { span_id: string; run_id: string; name: string; status: string; duration_ms?: number | null }
export interface Snapshot { messages: Message[]; runs: Run[]; tools: Tool[]; executor?: {available: boolean; heartbeat_at: number | null} }
export interface StreamEvent { v: number; run_id: string; seq: number; type: string; data: Record<string, unknown> }
export const terminal = (status: string) => ['completed', 'failed', 'interrupted'].includes(status);

export function reduceEvent(state: Snapshot, event: StreamEvent): Snapshot {
  const run = state.runs.find(r => r.id === event.run_id);
  if (!run || event.v !== 1 || event.seq <= run.last_seq) return state;
  let messages = state.messages;
  let tools = state.tools;
  let status = run.status;
  if (event.type === 'message.interim') {
    messages = messages.map(m => m.id === event.data.message_id ? {...m, phase: 'interim'} : m);
  } else if (event.type.startsWith('message.')) {
    const id = String(event.data.message_id);
    const previous = messages.find(m => m.id === id);
    const complete = event.type === 'message.completed';
    const next: Message = { id, run_id: run.id, role: 'assistant', phase: 'final',
      ...previous, content: complete ? String(event.data.text ?? '') : (previous?.content ?? '') + String(event.data.text ?? ''),
      status: complete ? 'completed' : 'draft' };
    messages = previous ? messages.map(m => m.id === id ? next : m) : [...messages, next];
  } else if (event.type.startsWith('tool.')) {
    const id = String(event.data.span_id);
    const next: Tool = { span_id: id, run_id: run.id, name: String(event.data.name),
      status: String(event.data.status ?? 'running'), duration_ms: event.data.duration_ms as number | undefined };
    tools = tools.some(t => t.span_id === id) ? tools.map(t => t.span_id === id ? next : t) : [...tools, next];
  } else if (event.type.startsWith('run.')) {
    status = event.type.slice(4);
    if (terminal(status)) messages = messages.map(m => m.run_id === run.id && m.status === 'draft' ? {...m, status} : m);
  }
  return { ...state, messages, tools, runs: state.runs.map(r => r.id === run.id ? {...r, status, last_seq: event.seq} : r) };
}
