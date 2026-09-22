import { loadConfig } from '../src/config.js';

export function testConfig(extra = {}) {
  return loadConfig({
    TELEGRAM_BOT_TOKEN: 'jeton-de-test',
    TELEGRAM_GROUP_CHAT_ID: '-1001234567890',
    ...extra,
  });
}

/** Faux client d'API : enregistre les appels au lieu de joindre Telegram. */
export function fakeClient(handlers = {}) {
  const calls = [];
  const client = new Proxy(
    {},
    {
      get(_target, method) {
        return async (params) => {
          const name = String(method);
          calls.push({ method: name, params });
          const handler = handlers[name];
          if (typeof handler === 'function') return handler(params, calls);
          return { message_id: calls.length };
        };
      },
    }
  );
  return { client, calls };
}

export const silentLogger = { log() {}, warn() {}, error() {} };

export function privateMessage(overrides = {}) {
  return {
    message_id: 11,
    chat: { id: 42, type: 'private', first_name: 'Jean' },
    from: { id: 42, is_bot: false, first_name: 'Jean', last_name: 'Dupont', username: 'jean' },
    text: 'Bonjour',
    ...overrides,
  };
}
