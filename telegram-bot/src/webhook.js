// Mode « webhook » : Telegram appelle une URL HTTPS publique.
// Utile sur un hébergeur qui exige un port HTTP ouvert (Render, Railway,
// Fly.io, reverse proxy nginx…).

import { createServer } from 'node:http';
import { ALLOWED_UPDATES } from './forwarder.js';

export async function runWebhook(config, bot, options = {}) {
  const { logger = console, signal } = options;
  if (!config.webhookUrl) {
    throw new Error('TRANSPORT=webhook exige WEBHOOK_URL (URL HTTPS publique de ce service).');
  }

  const url = new URL(config.webhookUrl);
  const path = url.pathname === '/' ? '/telegram' : url.pathname;
  url.pathname = path;

  const server = createServer((request, response) => {
    if (request.method === 'GET' && request.url === '/health') {
      response.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' });
      response.end('ok');
      return;
    }
    if (request.method !== 'POST' || request.url.split('?')[0] !== path) {
      response.writeHead(404).end();
      return;
    }
    if (
      config.webhookSecret &&
      request.headers['x-telegram-bot-api-secret-token'] !== config.webhookSecret
    ) {
      logger.warn('[webhook] jeton secret invalide — requête rejetée');
      response.writeHead(401).end();
      return;
    }

    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => {
      body += chunk;
      if (body.length > 5_000_000) request.destroy();
    });
    request.on('end', () => {
      // Telegram réessaie tant qu'il n'a pas de 200 : on accuse réception tout
      // de suite, le transfert se poursuit derrière.
      response.writeHead(200).end();
      let update;
      try {
        update = JSON.parse(body);
      } catch {
        logger.error('[webhook] corps de requête illisible');
        return;
      }
      bot.handleUpdate(update).catch((error) => logger.error(`[erreur] ${error?.message ?? error}`));
    });
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(config.port, () => resolve());
  });
  logger.log(`Serveur webhook à l'écoute sur le port ${config.port} (chemin ${path}).`);

  await bot.client.setWebhook({
    url: url.toString(),
    allowed_updates: ALLOWED_UPDATES,
    max_connections: 40,
    ...(config.webhookSecret ? { secret_token: config.webhookSecret } : {}),
  });
  logger.log(`Webhook enregistré auprès de Telegram : ${url.toString()}`);

  await new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    signal?.addEventListener('abort', resolve, { once: true });
  });

  await new Promise((resolve) => server.close(resolve));
  await bot.flushPending();
}
