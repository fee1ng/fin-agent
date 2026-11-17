// Adapt DSH session V4 events for the pinned LoongSuite 0.1.3 plugin.
// Only the observer receives translated copies; execution facts stay intact.
import { pathToFileURL } from 'node:url';

const entry = process.env.FIN_AGENT_LOONGSUITE_ENTRY
  || '/opt/fin-agent-observability/node_modules/@loongsuite/dsh-plugin/dist/index.js';
const upstream = await import(pathToFileURL(entry).href);

export const name = 'fin-agent-loongsuite';
export const inject = upstream.inject;
export const Config = upstream.Config;

export function normalizeEvent(event) {
  const message = event.data?.message;
  if (event.type !== 'tool/result' || message?.role !== 'tool') return event;
  return {
    ...event,
    data: {
      ...event.data,
      message: {
        ...message,
        source: { ...message.source, callId: message.toolCallId },
        content: [{ type: 'tool-result', toolCallId: message.toolCallId,
          content: message.content, isError: message.isError }],
      },
    },
  };
}

export function apply(ctx, config) {
  const observerContext = new Proxy(ctx, {
    get(target, property) {
      if (property === 'on') {
        return (eventName, listener) => target.on(eventName,
          eventName === 'session/event'
            ? (session, event) => listener(session, normalizeEvent(event))
            : listener);
      }
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
  // Explicitly override environment-based content capture for finance data.
  upstream.apply(observerContext, { ...config, captureContent: false });
}
