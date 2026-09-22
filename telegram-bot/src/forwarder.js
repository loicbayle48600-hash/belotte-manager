// Logique de transfert : quels messages relayer, et quels appels d'API
// produire pour les déposer dans le groupe. Fonctions pures et testables,
// aucun accès réseau ici.

/** Types de mise à jour qui portent un message à relayer. */
export const MESSAGE_UPDATES = [
  { key: 'message', edited: false },
  { key: 'edited_message', edited: true },
  { key: 'channel_post', edited: false },
  { key: 'edited_channel_post', edited: true },
];

export const ALLOWED_UPDATES = MESSAGE_UPDATES.map((entry) => entry.key);

export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/** Extrait le message d'une mise à jour, ou null si elle ne nous concerne pas. */
export function extractMessage(update) {
  for (const { key, edited } of MESSAGE_UPDATES) {
    const message = update?.[key];
    if (message) return { message, edited, kind: key };
  }
  return null;
}

/** Nom lisible d'un expéditeur (utilisateur, canal, ou anonyme). */
export function senderName(message) {
  const from = message?.from;
  if (from) {
    const name = [from.first_name, from.last_name].filter(Boolean).join(' ').trim();
    return name || from.username || `Utilisateur ${from.id}`;
  }
  const senderChat = message?.sender_chat;
  if (senderChat) return senderChat.title || senderChat.username || `Chat ${senderChat.id}`;
  return 'Expéditeur inconnu';
}

const CHAT_KINDS = {
  private: 'message privé',
  group: 'groupe',
  supergroup: 'groupe',
  channel: 'canal',
};

/**
 * En-tête HTML identifiant l'origine du message. Indispensable en mode copy
 * (le message recopié ne porte aucune trace de son auteur) et utile quand
 * l'expéditeur a masqué son compte dans les transferts.
 */
export function buildHeader(message, { edited = false } = {}) {
  const lines = [];
  const from = message?.from;
  const parts = [`💬 <b>${escapeHtml(senderName(message))}</b>`];
  if (from?.username) parts.push(`(@${escapeHtml(from.username)})`);
  const id = from?.id ?? message?.sender_chat?.id;
  if (id !== undefined) parts.push(`· <code>${escapeHtml(id)}</code>`);
  lines.push(parts.join(' '));

  const chat = message?.chat;
  if (chat && chat.type !== 'private') {
    const kind = CHAT_KINDS[chat.type] || chat.type;
    const title = chat.title ? ` « ${escapeHtml(chat.title)} »` : '';
    lines.push(`📍 ${kind}${title} · <code>${escapeHtml(chat.id)}</code>`);
  }

  if (edited) lines.push('✏️ message modifié après envoi');
  return lines.join('\n');
}

/**
 * Faut-il relayer ce message ? Renvoie la raison quand la réponse est non,
 * ce qui rend les journaux lisibles.
 */
export function shouldForward(message, config, { edited = false } = {}) {
  const chatId = message?.chat?.id;
  if (chatId === undefined) return { forward: false, reason: 'message sans conversation' };

  // Garde-fou principal : ne jamais relayer ce qui vient du groupe de
  // destination, sinon chaque message transféré se transfère lui-même.
  if (String(chatId) === String(config.groupChatId)) {
    return { forward: false, reason: 'message émis dans le groupe de destination' };
  }
  if (edited && !config.forwardEdits) return { forward: false, reason: 'message modifié (FORWARD_EDITS=false)' };
  if (config.onlyPrivate && message.chat.type !== 'private') {
    return { forward: false, reason: 'conversation non privée (ONLY_PRIVATE_CHATS=true)' };
  }
  if (config.ignoreBots && message.from?.is_bot) return { forward: false, reason: 'message envoyé par un bot' };
  return { forward: true, reason: '' };
}

function destinationParams(config) {
  const params = { chat_id: config.groupChatId };
  if (config.threadId !== null && config.threadId !== undefined) params.message_thread_id = config.threadId;
  if (config.silent) params.disable_notification = true;
  return params;
}

/**
 * Construit la suite d'appels d'API qui déposent un message — ou un album —
 * dans le groupe. `messages` doit provenir d'une seule et même conversation.
 *
 * @returns {{method: string, params: object}[]}
 */
export function buildForwardPlan(messages, config, options = {}) {
  const list = Array.isArray(messages) ? messages : [messages];
  if (list.length === 0) return [];
  const { edited = false, copy = false, header = null } = options;

  const useCopy = copy || config.mode === 'copy';
  const withHeader =
    header ?? (config.header === 'always' || (config.header === 'auto' && (useCopy || edited)));

  const first = list[0];
  const actions = [];

  if (withHeader) {
    actions.push({
      method: 'sendMessage',
      params: {
        ...destinationParams(config),
        text: buildHeader(first, { edited }),
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      },
    });
  }

  const ids = list.map((message) => message.message_id).sort((a, b) => a - b);
  const base = { ...destinationParams(config), from_chat_id: first.chat.id };

  if (ids.length > 1) {
    actions.push({
      method: useCopy ? 'copyMessages' : 'forwardMessages',
      params: { ...base, message_ids: ids },
    });
  } else {
    actions.push({
      method: useCopy ? 'copyMessage' : 'forwardMessage',
      params: { ...base, message_id: ids[0] },
    });
  }

  return actions;
}

/** Résumé d'une ligne pour les journaux. */
export function describeMessage(message, { edited = false } = {}) {
  const chat = message?.chat;
  const where = chat?.type === 'private' ? 'privé' : `${chat?.type} « ${chat?.title ?? ''} »`;
  const suffix = edited ? ' (modifié)' : '';
  return `${senderName(message)} [${where} ${chat?.id}] #${message?.message_id}${suffix}`;
}
