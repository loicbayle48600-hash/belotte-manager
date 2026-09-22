import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { loadConfig } from '../src/config.js';
import { parseEnvFile } from '../src/env-file.js';

test('la configuration minimale suffit', () => {
  const config = loadConfig({ TELEGRAM_BOT_TOKEN: 'a', TELEGRAM_GROUP_CHAT_ID: '-100123' });
  assert.equal(config.groupChatId, -100123);
  assert.equal(config.mode, 'forward');
  assert.equal(config.transport, 'polling');
  assert.equal(config.ignoreBots, true);
});

test('un jeton ou un groupe manquant donne un message explicite', () => {
  assert.throws(() => loadConfig({}), /TELEGRAM_BOT_TOKEN/);
  assert.throws(() => loadConfig({ TELEGRAM_BOT_TOKEN: 'a' }), /TELEGRAM_GROUP_CHAT_ID/);
});

test('une valeur invalide est refusée au démarrage', () => {
  assert.throws(
    () => loadConfig({ TELEGRAM_BOT_TOKEN: 'a', TELEGRAM_GROUP_CHAT_ID: '-1', FORWARD_MODE: 'clone' }),
    /FORWARD_MODE/
  );
  assert.throws(
    () => loadConfig({ TELEGRAM_BOT_TOKEN: 'a', TELEGRAM_GROUP_CHAT_ID: '-1', SILENT_FORWARD: 'peut-être' }),
    /SILENT_FORWARD/
  );
});

test('un nom public de canal est accepté comme destination', () => {
  const config = loadConfig({ TELEGRAM_BOT_TOKEN: 'a', TELEGRAM_GROUP_CHAT_ID: '@mon_canal' });
  assert.equal(config.groupChatId, '@mon_canal');
});

test('le fichier .env est lu, commentaires et guillemets compris', () => {
  const values = parseEnvFile(
    ['# commentaire', 'TELEGRAM_BOT_TOKEN=123:AA', 'export FORWARD_MODE="copy"', 'PORT=8080 # port', ''].join('\n')
  );
  assert.deepEqual(values, {
    TELEGRAM_BOT_TOKEN: '123:AA',
    FORWARD_MODE: 'copy',
    PORT: '8080',
  });
});
