#!/usr/bin/env node
// Bot Telegram : transfère vers un groupe tous les messages qu'il reçoit.
// Démarrage : npm start (après avoir rempli le fichier .env).

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnvFile } from './src/env-file.js';
import { loadConfig } from './src/config.js';
import { createClient, TelegramError } from './src/api.js';
import { createBot } from './src/bot.js';
import { runPolling } from './src/polling.js';
import { runWebhook } from './src/webhook.js';

const here = dirname(fileURLToPath(import.meta.url));
loadEnvFile(join(here, '.env'));

async function main() {
  const config = loadConfig();
  const client = createClient(config.token);

  let me;
  try {
    me = await client.getMe({});
  } catch (error) {
    if (error instanceof TelegramError && error.code === 401) {
      throw new Error('Jeton refusé par Telegram : vérifiez TELEGRAM_BOT_TOKEN auprès de @BotFather.');
    }
    throw error;
  }
  console.log(`Bot connecté : @${me.username} (${me.first_name}).`);

  // Vérification d'accès au groupe : mieux vaut échouer ici, au démarrage,
  // qu'à la réception du premier message.
  try {
    const chat = await client.getChat({ chat_id: config.groupChatId });
    console.log(`Destination : ${chat.title ?? chat.id} (${chat.type}, ${chat.id}).`);
  } catch (error) {
    if (error instanceof TelegramError && error.isPermanent) {
      throw new Error(
        `Groupe de destination inaccessible (${error.description}). ` +
          'Vérifiez que le bot est bien membre du groupe et que TELEGRAM_GROUP_CHAT_ID est correct ' +
          '(envoyez /id dans le groupe pour l’obtenir).'
      );
    }
    throw error;
  }

  await client
    .setMyCommands({
      commands: [
        { command: 'start', description: 'Commencer la conversation' },
        { command: 'id', description: 'Afficher l’identifiant de la conversation' },
      ],
    })
    .catch(() => {});

  const bot = createBot(config, { client });
  const controller = new AbortController();
  let stopping = false;
  for (const signalName of ['SIGINT', 'SIGTERM']) {
    process.on(signalName, () => {
      if (stopping) process.exit(1);
      stopping = true;
      console.log('\nArrêt en cours…');
      controller.abort();
    });
  }

  const mode = config.mode === 'copy' ? 'copie sans bandeau' : 'transfert direct';
  console.log(`Mode : ${mode}. Transport : ${config.transport}.`);

  if (config.transport === 'webhook') {
    await runWebhook(config, bot, { signal: controller.signal });
  } else {
    await runPolling(config, bot, { signal: controller.signal });
  }
  console.log('Bot arrêté.');
}

main().catch((error) => {
  console.error(`\n❌ ${error?.message ?? error}`);
  process.exitCode = 1;
});
