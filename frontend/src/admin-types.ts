export interface AdminRun {
  id: string; user_id: string; username: string; session_id: string; title: string; status: string;
  received_at: number; started_at: number | null; finished_at: number | null; engine: string;
  queue_ms: number | null; execution_ms: number | null; total_ms: number | null; first_content_ms: number | null;
  input_preview: string; tool_calls: number; failed_tools: number; slowest_tool_ms: number | null;
}
export interface Call {
  id: string; kind: string; name: string; parent_id: string | null; tool_call_id: string | null;
  started_at: number | null; ended_at: number | null; offset_ms: number | null; duration_ms: number | null;
  status: string; first_chunk_ms: number | null; first_chunk_kind: string | null;
  usage: Record<string, number | null> | null; arguments: unknown; summary: string | null; error: unknown;
}
export interface AdminDetail {
  run: AdminRun; input: {message_id: string | null; text: string}; spans: Call[];
  answers: {id: string; content: string; phase: string; status: string}[];
  usage: Record<string, number | null>; model_calls: number; tool_calls: number; failed_tools: number;
  trace_state: string; slowest_span_id: string | null; coverage: string; browser_first_render_ms: number | null;
}
