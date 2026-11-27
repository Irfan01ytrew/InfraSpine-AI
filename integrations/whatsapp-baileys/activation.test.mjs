import test from 'node:test';
import assert from 'node:assert/strict';
import { activationFor } from './activation.mjs';

test('supports natural names and the existing command prefix', () => {
  for (const text of ['Irfanix, laptop check karo', 'عرفانکس، laptop check karo', '/agent laptop check karo']) {
    assert.equal(activationFor(text, 100, 0)?.text, 'laptop check karo');
  }
  assert.equal(activationFor('Irfanix', 100, 0)?.text, 'Hello');
  assert.equal(activationFor('IRFANIXplus hi', 100, 0), null);
});

test('follow-ups expire and generated messages never become commands', () => {
  const { activeUntil } = activationFor('Irfanix, laptop status', 100, 0);
  assert.equal(activationFor('detail btao', 101, activeUntil)?.text, 'detail btao');
  assert.equal(activationFor('detail btao', activeUntil, activeUntil), null);
  assert.equal(activationFor('InfraSpine AI · IRFANIX\n\n/agent do something', 101, activeUntil), null);
  assert.equal(activationFor('a'.repeat(6001), 101, activeUntil), null);
});
