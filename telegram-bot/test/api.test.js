import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { callApi, TelegramError } from '../src/api.js';

function response(payload) {
  return { json: async () => payload };
}

test('une réponse valide renvoie le résultat', async () => {
  const result = await callApi('jeton', 'getMe', {}, {
    fetchImpl: async () => response({ ok: true, result: { username: 'mon_bot' } }),
  });
  assert.equal(result.username, 'mon_bot');
});

test('une erreur 400 est définitive et n’est pas rejouée', async () => {
  let attempts = 0;
  await assert.rejects(
    callApi('jeton', 'forwardMessage', {}, {
      fetchImpl: async () => {
        attempts += 1;
        return response({ ok: false, error_code: 400, description: 'Bad Request: chat not found' });
      },
    }),
    (error) => error instanceof TelegramError && error.isPermanent
  );
  assert.equal(attempts, 1);
});

test('une limite de débit est respectée puis la requête est rejouée', async () => {
  let attempts = 0;
  const result = await callApi('jeton', 'sendMessage', {}, {
    fetchImpl: async () => {
      attempts += 1;
      if (attempts === 1) {
        return response({ ok: false, error_code: 429, description: 'Too Many Requests', parameters: { retry_after: 0 } });
      }
      return response({ ok: true, result: { message_id: 1 } });
    },
  });
  assert.equal(attempts, 2);
  assert.equal(result.message_id, 1);
});

test('une coupure réseau est réessayée', async () => {
  let attempts = 0;
  const result = await callApi('jeton', 'getUpdates', {}, {
    attempts: 3,
    fetchImpl: async () => {
      attempts += 1;
      if (attempts < 2) throw new Error('ECONNRESET');
      return response({ ok: true, result: [] });
    },
  });
  assert.deepEqual(result, []);
  assert.equal(attempts, 2);
});
