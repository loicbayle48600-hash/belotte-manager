// Mode « long polling » : le bot interroge Telegram en boucle.
// Aucune URL publique n'est nécessaire — il tourne aussi bien sur un Raspberry
// Pi, un petit VPS ou un poste laissé allumé.

import { ALLOWED_UPDATES } from './forwarder.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function runPolling(config, bot, options = {}) {
  const { logger = console, signal, pollTimeout = 50 } = options;
  const client = bot.client;

  // Un webhook actif empêche getUpdates : on le retire au démarrage.
  await client.deleteWebhook({ drop_pending_updates: false }, { signal }).catch(() => {});

  let offset = 0;
  let failures = 0;
  logger.log('Écoute des messages (long polling)…');

  while (!signal?.aborted) {
    try {
      const updates = await client.getUpdates(
        { offset, timeout: pollTimeout, allowed_updates: ALLOWED_UPDATES },
        { timeoutMs: (pollTimeout + 20) * 1000, attempts: 1, signal }
      );
      failures = 0;
      for (const update of updates) {
        offset = update.update_id + 1;
        await bot.handleUpdate(update);
      }
    } catch (error) {
      if (signal?.aborted) break;
      failures += 1;
      const wait = Math.min(2 ** failures, 30);
      logger.error(`[réseau] ${error?.description ?? error?.message ?? error} — nouvelle tentative dans ${wait} s`);
      await sleep(wait * 1000);
    }
  }

  await bot.flushPending();
}
