import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import {
  buildForwardPlan,
  buildHeader,
  extractMessage,
  senderName,
  shouldForward,
} from '../src/forwarder.js';
import { privateMessage, testConfig } from './helpers.js';

test('extractMessage reconnaît les quatre types de mise à jour', () => {
  assert.equal(extractMessage({ update_id: 1, message: privateMessage() }).edited, false);
  assert.equal(extractMessage({ edited_message: privateMessage() }).edited, true);
  assert.equal(extractMessage({ channel_post: privateMessage() }).kind, 'channel_post');
  assert.equal(extractMessage({ callback_query: {} }), null);
});

test('un message privé est transféré tel quel', () => {
  const config = testConfig();
  const plan = buildForwardPlan([privateMessage()], config);
  assert.deepEqual(plan, [
    {
      method: 'forwardMessage',
      params: { chat_id: -1001234567890, from_chat_id: 42, message_id: 11 },
    },
  ]);
});

test('le mode copy ajoute un en-tête identifiant l’expéditeur', () => {
  const config = testConfig({ FORWARD_MODE: 'copy' });
  const plan = buildForwardPlan([privateMessage()], config);
  assert.equal(plan.length, 2);
  assert.equal(plan[0].method, 'sendMessage');
  assert.match(plan[0].params.text, /Jean Dupont/);
  assert.match(plan[0].params.text, /@jean/);
  assert.equal(plan[1].method, 'copyMessage');
});

test('un album part en un seul appel groupé', () => {
  const config = testConfig();
  const album = [
    privateMessage({ message_id: 31, media_group_id: '777' }),
    privateMessage({ message_id: 30, media_group_id: '777' }),
  ];
  const plan = buildForwardPlan(album, config);
  assert.deepEqual(plan[0], {
    method: 'forwardMessages',
    params: { chat_id: -1001234567890, from_chat_id: 42, message_ids: [30, 31] },
  });
});

test('le sujet et le mode silencieux sont reportés sur chaque envoi', () => {
  const config = testConfig({ TELEGRAM_GROUP_THREAD_ID: '17', SILENT_FORWARD: 'true' });
  const [action] = buildForwardPlan([privateMessage()], config);
  assert.equal(action.params.message_thread_id, 17);
  assert.equal(action.params.disable_notification, true);
});

test('les messages venant du groupe de destination ne sont jamais relayés', () => {
  const config = testConfig();
  const message = privateMessage({ chat: { id: -1001234567890, type: 'supergroup', title: 'Équipe' } });
  assert.equal(shouldForward(message, config).forward, false);
});

test('les filtres bots, privé et messages modifiés sont respectés', () => {
  assert.equal(
    shouldForward(privateMessage({ from: { id: 9, is_bot: true, first_name: 'Robot' } }), testConfig())
      .forward,
    false
  );
  const grouped = privateMessage({ chat: { id: -100999, type: 'supergroup', title: 'Autre' } });
  assert.equal(shouldForward(grouped, testConfig({ ONLY_PRIVATE_CHATS: 'true' })).forward, false);
  assert.equal(shouldForward(grouped, testConfig()).forward, true);
  assert.equal(
    shouldForward(privateMessage(), testConfig({ FORWARD_EDITS: 'false' }), { edited: true }).forward,
    false
  );
});

test('un message modifié est signalé comme tel dans l’en-tête', () => {
  const plan = buildForwardPlan([privateMessage()], testConfig(), { edited: true });
  assert.match(plan[0].params.text, /modifié/);
});

test('l’en-tête échappe le HTML des noms', () => {
  const message = privateMessage({ from: { id: 7, first_name: '<b>Pirate</b>' } });
  assert.match(buildHeader(message), /&lt;b&gt;Pirate&lt;\/b&gt;/);
  assert.equal(senderName({ sender_chat: { id: -1, title: 'Canal' } }), 'Canal');
});

test('un message de groupe affiche le nom du groupe dans l’en-tête', () => {
  const message = privateMessage({ chat: { id: -100999, type: 'supergroup', title: 'Cuisine' } });
  const header = buildHeader(message);
  assert.match(header, /groupe « Cuisine »/);
});
