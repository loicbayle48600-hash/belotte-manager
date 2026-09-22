// Appels à l'API Bot Telegram (https://core.telegram.org/bots/api).
// Uniquement fetch, présent nativement à partir de Node 18.

const API_ROOT = 'https://api.telegram.org';

export class TelegramError extends Error {
  constructor(method, { error_code: code, description, parameters } = {}) {
    super(`${method} a échoué (${code ?? '?'}) : ${description ?? 'erreur inconnue'}`);
    this.name = 'TelegramError';
    this.method = method;
    this.code = code;
    this.description = description ?? '';
    this.parameters = parameters ?? {};
  }

  /** Erreur définitive : rejouer la même requête ne changera rien. */
  get isPermanent() {
    return this.code >= 400 && this.code < 500 && this.code !== 429;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Appelle une méthode de l'API. Réessaie sur 429 (limite de débit, en
 * respectant retry_after) et sur les erreurs réseau / 5xx.
 */
export async function callApi(token, method, params = {}, options = {}) {
  const { attempts = 4, timeoutMs = 90_000, fetchImpl = fetch, signal: externalSignal } = options;
  let lastError;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    // L'arrêt du bot (Ctrl-C, SIGTERM) doit couper la requête longue en cours.
    const signal =
      externalSignal && typeof AbortSignal.any === 'function'
        ? AbortSignal.any([controller.signal, externalSignal])
        : controller.signal;
    try {
      if (externalSignal?.aborted) throw new Error('appel interrompu');
      const response = await fetchImpl(`${API_ROOT}/bot${token}/${method}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(params),
        signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (payload && payload.ok) return payload.result;

      const error = new TelegramError(method, payload);
      if (error.code === 429) {
        const wait = Number(error.parameters.retry_after ?? 1);
        if (attempt < attempts) {
          await sleep(Math.min(wait, 60) * 1000);
          lastError = error;
          continue;
        }
      }
      if (error.isPermanent) throw error;
      lastError = error;
    } catch (error) {
      if (error instanceof TelegramError && error.isPermanent) throw error;
      if (externalSignal?.aborted) throw error;
      lastError = error;
    } finally {
      clearTimeout(timer);
    }

    if (externalSignal?.aborted) break;
    if (attempt < attempts) await sleep(Math.min(2 ** attempt, 16) * 1000);
  }

  throw lastError;
}

/** Client lié à un jeton : client.sendMessage({ chat_id, text }). */
export function createClient(token, options = {}) {
  return new Proxy(
    {},
    {
      get(_target, method) {
        return (params, callOptions) => callApi(token, String(method), params, { ...options, ...callOptions });
      },
    }
  );
}
