// Public Cordis waterfalls and SessionStore.flush from the pinned rc.2 runtime.
// The host treats the watermark as an acknowledgement, never SDK idle alone.
import { mkdir, open, rename } from 'node:fs/promises';
import { join } from 'node:path';

export const name = 'fin-agent-checkpoint';
export const inject = ['sessions', 'llm', 'tools'];

export function apply(ctx, config) {
  const sessions = new Map();
  let failure;
  let pending = Promise.resolve();
  async function checkpoint(session, sequence) {
    if (failure) throw failure;
    if (!await ctx.sessions.flush(session)) throw new Error('storage_error: no persistence backend');
    const directory = config.ackDirectory;
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const target = join(directory, `${session.id}.json`);
    const temporary = `${target}.tmp`;
    const file = await open(temporary, 'w', 0o600);
    try {
      await file.writeFile(JSON.stringify({ session_id: session.id, sequence }));
      await file.sync();
    } finally { await file.close(); }
    await rename(temporary, target);
    const parent = await open(directory, 'r');
    try { await parent.sync(); } finally { await parent.close(); }
  }
  function enqueue(session, sequence) {
    pending = pending.then(() => checkpoint(session, sequence));
    // Retain failure for every subsequent execution gate, while containing
    // rejection of the fire-and-forget session notification itself.
    pending.catch(error => { failure = error; });
    return pending;
  }
  ctx.on('session/event', (session, event) => {
    sessions.set(session.id, { session, sequence: event.seq });
    if (event.type === 'turn/end' || event.type === 'tool/result') {
      enqueue(session, event.seq);
    }
  });
  async function barrier() {
    if (failure) throw failure;
    for (const { session, sequence } of sessions.values()) await enqueue(session, sequence);
    await pending;
  }
  ctx.on('llm/stream', async function* (_options, next) {
    await barrier();
    yield* next();
  });
  ctx.on('tools/execute', async (_execution, next) => {
    await barrier();
    return next();
  });
  ctx.on('session/disposed', session => { sessions.delete(session.id); });
}
