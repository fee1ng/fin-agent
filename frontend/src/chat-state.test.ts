import { describe, expect, it } from 'vitest';
import { reduceEvent, type Snapshot, type StreamEvent } from './chat-state';

const initial: Snapshot = {messages: [], tools: [], runs: [{id: 'r', session_id: 's', status: 'running', last_seq: 1}]};
function event(seq: number, type: string, data: Record<string, unknown>): StreamEvent { return {v: 1, run_id: 'r', seq, type, data}; }

describe('stream consumption', () => {
  it('ignores replay and replaces the draft with the authoritative final message', () => {
    const part = event(2, 'message.delta', {message_id: 'm', text: '回答'});
    const draft = reduceEvent(initial, part);
    expect(reduceEvent(draft, part)).toBe(draft);
    const complete = reduceEvent(draft, event(3, 'message.completed', {message_id: 'm', text: '完整回答'}));
    expect(complete.messages[0].content).toBe('完整回答');
    expect(reduceEvent(complete, event(4, 'run.completed', {})).runs[0].status).toBe('completed');
  });
  it('separates model commentary from final output and preserves per-call tool identity', () => {
    let state = reduceEvent(initial, event(2, 'message.delta', {message_id: 'interim', text: '开始查询'}));
    state = reduceEvent(state, event(3, 'message.interim', {message_id: 'interim'}));
    state = reduceEvent(state, event(4, 'tool.started', {span_id: 't1', name: 'get_daily_price'}));
    state = reduceEvent(state, event(5, 'tool.started', {span_id: 't2', name: 'get_daily_price'}));
    state = reduceEvent(state, event(6, 'message.completed', {message_id: 'final', text: '结果'}));
    expect(state.messages.map(m => m.phase)).toEqual(['interim', 'final']);
    expect(state.tools.length).toBe(2);
  });
});
