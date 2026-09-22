// Orchestration : reçoit les mises à jour Telegram, regroupe les albums,
// exécute le plan de transfert et répond aux commandes de service.

import { createClient, TelegramError } from './api.js';
import {
  buildForwardPlan,
  buildHeader,
  describeMessage,
  extractMessage,
  shouldForward,
} from './forwarder.js';

// Un album (plusieurs photos envoyées d'un coup) arrive en plusieurs mises à
// jour partageant le même media_group_id : on attend les suivantes avant de
// transférer, pour que le groupe reçoive l'album entier et non des photos
// éparpillées.
const ALBUM_WAIT_MS = 1200;

export function createBot(config, options = {}) {
  const client = options.client ?? createClient(config.token);
  const logger = options.logger ?? console;
  const albumWaitMs = options.albumWaitMs ?? ALBUM_WAIT_MS;

  const albums = new Map();
  // File d'attente : les messages sont déposés dans le groupe dans l'ordre où
  // ils arrivent, jamais en parallèle.
  let queue = Promise.resolve();

  function enqueue(task) {
    queue = queue.then(task).catch((error) => {
      logger.error(`[erreur] ${error?.message ?? error}`);
    });
    return queue;
  }

  async function runPlan(plan) {
    for (const action of plan) {
      await client[action.method](action.params);
    }
  }

  async function deliver(messages, { edited }) {
    const first = messages[0];
    const label = describeMessage(first, { edited });
    try {
      await runPlan(buildForwardPlan(messages, config, { edited }));
      logger.log(`[transféré] ${label}${messages.length > 1 ? ` (album de ${messages.length})` : ''}`);
      return;
    } catch (error) {
      if (!(error instanceof TelegramError) || !error.isPermanent || config.mode === 'copy') throw error;
      // Transfert direct refusé (contenu protégé, confidentialité du compte…) :
      // on recopie le message en l'accompagnant de son en-tête d'origine.
      logger.warn(`[repli copie] ${label} — ${error.description}`);
    }

    try {
      await runPlan(buildForwardPlan(messages, config, { edited, copy: true, header: true }));
      logger.log(`[copié] ${label}`);
    } catch (error) {
      logger.error(`[échec] ${label} — ${error?.description ?? error?.message ?? error}`);
      await notifyFailure(first, edited, error).catch(() => {});
    }
  }

  async function notifyFailure(message, edited, error) {
    const reason = error?.description || error?.message || 'raison inconnue';
    await client.sendMessage({
      chat_id: config.groupChatId,
      ...(config.threadId !== null && config.threadId !== undefined
        ? { message_thread_id: config.threadId }
        : {}),
      text: `${buildHeader(message, { edited })}\n\n⚠️ Message reçu mais non relayable : ${reason}`,
      parse_mode: 'HTML',
      link_preview_options: { is_disabled: true },
    });
  }

  function scheduleAlbum(message, edited) {
    const key = `${message.chat.id}:${message.media_group_id}`;
    let album = albums.get(key);
    if (!album) {
      album = { messages: [], edited, timer: null };
      albums.set(key, album);
    }
    album.messages.push(message);
    if (album.timer) clearTimeout(album.timer);
    album.timer = setTimeout(() => flushAlbum(key), albumWaitMs);
    if (typeof album.timer.unref === 'function') album.timer.unref();
  }

  function flushAlbum(key) {
    const album = albums.get(key);
    if (!album) return;
    albums.delete(key);
    if (album.timer) clearTimeout(album.timer);
    // Telegram limite forwardMessages/copyMessages à 100 messages par appel.
    const batches = [];
    for (let index = 0; index < album.messages.length; index += 100) {
      batches.push(album.messages.slice(index, index + 100));
    }
    for (const batch of batches) enqueue(() => deliver(batch, { edited: album.edited }));
  }

  /** Vide les albums encore en attente (arrêt du bot, tests). */
  function flushPending() {
    for (const key of [...albums.keys()]) flushAlbum(key);
    return queue;
  }

  function commandName(message) {
    const text = message.text ?? message.caption ?? '';
    const match = /^\/([A-Za-z0-9_]+)(@[A-Za-z0-9_]+)?\b/.exec(text.trim());
    return match ? match[1].toLowerCase() : null;
  }

  async function reply(message, text) {
    await client.sendMessage({
      chat_id: message.chat.id,
      ...(message.message_thread_id ? { message_thread_id: message.message_thread_id } : {}),
      text,
      parse_mode: 'HTML',
      link_preview_options: { is_disabled: true },
    });
  }

  async function handleCommand(message) {
    const command = commandName(message);
    if (!command) return false;

    if (command === 'id') {
      const isTarget = String(message.chat.id) === String(config.groupChatId);
      const lines = [`🆔 Identifiant de cette conversation : <code>${message.chat.id}</code>`];
      if (message.chat.type !== 'private') {
        lines.push(
          isTarget
            ? 'C’est déjà le groupe de destination configuré.'
            : 'À recopier dans <code>TELEGRAM_GROUP_CHAT_ID</code> pour en faire le groupe de destination.'
        );
        if (message.message_thread_id) {
          lines.push(`Sujet courant : <code>${message.message_thread_id}</code> (TELEGRAM_GROUP_THREAD_ID)`);
        }
      }
      await reply(message, lines.join('\n'));
      return true;
    }

    if (command === 'start' && message.chat.type === 'private') {
      await reply(
        message,
        'Bonjour ! Écrivez-moi ici : tous vos messages (texte, photos, documents, vocaux…) sont transférés à l’équipe.'
      );
      return true;
    }

    return false;
  }

  /** Traite une mise à jour Telegram. */
  async function handleUpdate(update) {
    const extracted = extractMessage(update);
    if (!extracted) return;
    const { message, edited } = extracted;

    // Les commandes de service répondent partout, y compris dans le groupe de
    // destination : c'est ainsi qu'on récupère son identifiant.
    try {
      await handleCommand(message);
    } catch (error) {
      logger.error(`[commande] ${error?.message ?? error}`);
    }

    const verdict = shouldForward(message, config, { edited });
    if (!verdict.forward) {
      logger.log(`[ignoré] ${describeMessage(message, { edited })} — ${verdict.reason}`);
      return;
    }

    if (message.media_group_id) {
      scheduleAlbum(message, edited);
      return;
    }

    await enqueue(() => deliver([message], { edited }));
  }

  return { handleUpdate, flushPending, client };
}
