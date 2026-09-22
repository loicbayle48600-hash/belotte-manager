// Lecture et validation de la configuration (variables d'environnement).
// Aucun secret n'est écrit dans le dépôt : le jeton vient toujours de
// l'environnement (fichier .env local, secret d'hébergeur, unité systemd…).

const TRUE_VALUES = new Set(['1', 'true', 'oui', 'yes', 'on']);
const FALSE_VALUES = new Set(['0', 'false', 'non', 'no', 'off']);

function readBoolean(value, fallback, name) {
  if (value === undefined || value === '') return fallback;
  const normalized = String(value).trim().toLowerCase();
  if (TRUE_VALUES.has(normalized)) return true;
  if (FALSE_VALUES.has(normalized)) return false;
  throw new Error(`${name} doit valoir true ou false (reçu « ${value} »).`);
}

function readChoice(value, choices, fallback, name) {
  if (value === undefined || value === '') return fallback;
  const normalized = String(value).trim().toLowerCase();
  if (!choices.includes(normalized)) {
    throw new Error(`${name} doit valoir ${choices.join(' ou ')} (reçu « ${value} »).`);
  }
  return normalized;
}

function readInteger(value, fallback, name) {
  if (value === undefined || value === '') return fallback;
  const parsed = Number(String(value).trim());
  if (!Number.isInteger(parsed)) {
    throw new Error(`${name} doit être un nombre entier (reçu « ${value} »).`);
  }
  return parsed;
}

/**
 * Construit la configuration du bot à partir d'un objet d'environnement.
 * Lève une erreur explicite (en français) si une valeur obligatoire manque.
 */
export function loadConfig(env = process.env) {
  const token = (env.TELEGRAM_BOT_TOKEN || '').trim();
  if (!token) {
    throw new Error(
      'TELEGRAM_BOT_TOKEN est vide : copiez .env.example en .env et collez-y le jeton donné par @BotFather.'
    );
  }

  const rawGroupId = (env.TELEGRAM_GROUP_CHAT_ID || '').trim();
  if (!rawGroupId) {
    throw new Error(
      "TELEGRAM_GROUP_CHAT_ID est vide : ajoutez le bot au groupe, envoyez-y /id et recopiez l'identifiant affiché."
    );
  }
  // Les identifiants de groupe sont négatifs (-100…) ; on accepte aussi un
  // @nom_public de canal/supergroupe, que l'API Telegram comprend directement.
  const groupChatId = /^-?\d+$/.test(rawGroupId) ? Number(rawGroupId) : rawGroupId;
  if (typeof groupChatId === 'number' && !Number.isSafeInteger(groupChatId)) {
    throw new Error(`TELEGRAM_GROUP_CHAT_ID n'est pas un identifiant valide (reçu « ${rawGroupId} »).`);
  }

  return {
    token,
    groupChatId,
    // forward : message transféré tel quel (bandeau « Transféré de … »).
    // copy    : message recopié sans bandeau, précédé de l'en-tête d'origine.
    mode: readChoice(env.FORWARD_MODE, ['forward', 'copy'], 'forward', 'FORWARD_MODE'),
    // auto : en-tête ajouté en mode copy et quand le transfert direct échoue.
    header: readChoice(env.FORWARD_HEADER, ['auto', 'always', 'never'], 'auto', 'FORWARD_HEADER'),
    // Sujet (topic) du groupe où déposer les messages, si le groupe en utilise.
    threadId: readInteger(env.TELEGRAM_GROUP_THREAD_ID, null, 'TELEGRAM_GROUP_THREAD_ID'),
    // Ne relayer que les conversations privées (ignore groupes et canaux).
    onlyPrivate: readBoolean(env.ONLY_PRIVATE_CHATS, false, 'ONLY_PRIVATE_CHATS'),
    // Les messages des autres bots sont ignorés par défaut (évite les boucles).
    ignoreBots: readBoolean(env.IGNORE_BOTS, true, 'IGNORE_BOTS'),
    // Relayer aussi les messages modifiés après coup.
    forwardEdits: readBoolean(env.FORWARD_EDITS, true, 'FORWARD_EDITS'),
    // Notifications silencieuses dans le groupe.
    silent: readBoolean(env.SILENT_FORWARD, false, 'SILENT_FORWARD'),
    // polling : le bot interroge Telegram (aucune URL publique nécessaire).
    // webhook : Telegram appelle le bot (nécessite une URL HTTPS publique).
    transport: readChoice(env.TRANSPORT, ['polling', 'webhook'], 'polling', 'TRANSPORT'),
    webhookUrl: (env.WEBHOOK_URL || '').trim(),
    webhookSecret: (env.WEBHOOK_SECRET || '').trim(),
    port: readInteger(env.PORT, 3000, 'PORT'),
  };
}
