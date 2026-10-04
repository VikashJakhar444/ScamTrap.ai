'use strict';

const assert = require('node:assert/strict');
const { resolveSenderNumber } = require('../sender_identity');

async function run() {
    const lidMessage = {
        from: '213142500053021@lid',
        getContact: async () => ({
            number: '213142500053021',
            id: { user: '213142500053021' }
        })
    };
    const lidClient = {
        getContactLidAndPhone: async (ids) => {
            assert.deepEqual(ids, ['213142500053021@lid']);
            return [{ lid: '213142500053021@lid', pn: '6204257765@c.us' }];
        }
    };
    assert.equal(await resolveSenderNumber(lidMessage, lidClient), '6204257765');

    const unresolvedClient = {
        getContactLidAndPhone: async () => [{ lid: '213142500053021@lid', pn: null }]
    };
    assert.equal(await resolveSenderNumber(lidMessage, unresolvedClient), '');

    const phoneMessage = {
        from: '6204257765@c.us',
        getContact: async () => ({ number: '6204257765' })
    };
    assert.equal(await resolveSenderNumber(phoneMessage, {}), '6204257765');

    console.log('PASS: WhatsApp LID resolves to its real phone number; unresolved LIDs are not phone numbers.');
}

run().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
