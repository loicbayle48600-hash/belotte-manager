import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { createBot } from '../src/bot.js';
import { TelegramError } from '../src/api.js';
import { fakeClient, privateMessage, silentLogger, testConfig } from './helpers.js';

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function makeBot(config, handlers = {}, options = {}) {
  const { client, calls } = fakeClient(handlers);
  const bot = createBot(config, { client, logger: silentLogger, albumWaitMs: 5, ...options });
  return { bot, calls };
}

test('un message reçu part vers le groupe', async () => {
  const { bot, calls } = makeBot(testConfig());
  await bot.handleUpdate({ update_id: 1, message: privateMessage() });
  await bot.flushPending();
  assert.deepEqual(calls, [
    {
      method: 'forwardMessage',
      params: { chat_id: -1001234567890, from_chat_id: 42, message_id: 11 },
    },
  ]);
});

test('un album est regroupé en un seul transfert', async () => {
  const { bot, calls } = makeBot(testConfig());
  for (const id of [21, 22, 23]) {
    await bot.handleUpdate({
      update_id: id,
      message: privateMessage({ message_id: id, media_group_id: '555', text: undefined, photo: [] }),
    });
  }
  await wait(20);
  await bot.flushPending();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'forwardMessages');
  assert.deepEqual(calls[0].params.message_ids, [21, 22, 23]);
});

test('si le transfert direct est refusé, le message est recopié avec son en-tête', async () => {
  const { bot, calls } = makeBot(testConfig(), {
    forwardMessage() {
      throw new TelegramError('forwardMessage', {
        error_code: 400,
        description: 'Bad Request: message can\'t be forwarded',
      });
    },
  });
  await bot.handleUpdate({ update_id: 2, message: privateMessage() });
  await bot.flushPending();
  assert.deepEqual(
    calls.map((call) => call.method),
    ['forwardMessage', 'sendMessage', 'copyMessage']
  );
  assert.match(calls[1].params.text, /Jean Dupont/);
});

test('un échec total prévient le groupe au lieu de perdre le message', async () => {
  const fail = () => {
    throw new TelegramError('x', { error_code: 400, description: 'Bad Request: refusé' });
  };
  const { bot, calls } = makeBot(testConfig(), {
    forwardMessage: fail,
    copyMessage: fail,
  });
  await bot.handleUpdate({ update_id: 3, message: privateMessage() });
  await bot.flushPending();
  const last = calls.at(-1);
  assert.equal(last.method, 'sendMessage');
  assert.equal(last.params.chat_id, -1001234567890);
  assert.match(last.params.text, /non relayable/);
});

test('/id répond dans le groupe de destination sans rien transférer', async () => {
  const { bot, calls } = makeBot(testConfig());
  await bot.handleUpdate({
    update_id: 4,
    message: privateMessage({
      message_id: 60,
      text: '/id@mon_bot',
      chat: { id: -1001234567890, type: 'supergroup', title: 'Équipe' },
    }),
  });
  await bot.flushPending();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'sendMessage');
  assert.equal(calls[0].params.chat_id, -1001234567890);
  assert.match(calls[0].params.text, /-1001234567890/);
});

test('une commande envoyée en privé est relayée comme les autres messages', async () => {
  const { bot, calls } = makeBot(testConfig());
  await bot.handleUpdate({ update_id: 5, message: privateMessage({ text: '/start' }) });
  await bot.flushPending();
  assert.deepEqual(
    calls.map((call) => call.method),
    ['sendMessage', 'forwardMessage']
  );
  assert.equal(calls[0].params.chat_id, 42);
});

test('les messages du groupe de destination ne repartent pas en boucle', async () => {
  const { bot, calls } = makeBot(testConfig());
  await bot.handleUpdate({
    update_id: 6,
    message: privateMessage({
      text: 'réponse de l’équipe',
      chat: { id: -1001234567890, type: 'supergroup', title: 'Équipe' },
    }),
  });
  await bot.flushPending();
  assert.deepEqual(calls, []);
});

test('les messages arrivent dans le groupe dans leur ordre d’envoi', async () => {
  const order = [];
  const { bot } = makeBot(testConfig(), {
    async forwardMessage(params) {
      const delay = params.message_id === 1 ? 20 : 1;
      await wait(delay);
      order.push(params.message_id);
      return {};
    },
  });
  bot.handleUpdate({ update_id: 7, message: privateMessage({ message_id: 1 }) });
  bot.handleUpdate({ update_id: 8, message: privateMessage({ message_id: 2 }) });
  await wait(60);
  await bot.flushPending();
  assert.deepEqual(order, [1, 2]);
});
