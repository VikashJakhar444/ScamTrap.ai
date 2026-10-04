'use strict';

function phoneDigits(value) {
    const local = String(value || '').split('@')[0].split(':')[0];
    const digits = local.replace(/\D/g, '');
    return digits.length >= 10 && digits.length <= 13 ? digits : '';
}

async function resolveSenderNumber(msg, client) {
    const from = msg.from || '';
    const isLid = from.endsWith('@lid');
    const jidDigits = from.split('@')[0].split(':')[0].replace(/\D/g, '');

    if (isLid) {
        try {
            const [mapping] = await client.getContactLidAndPhone([from]);
            const mappedPhone = phoneDigits(mapping && mapping.pn);
            if (mappedPhone) return mappedPhone;
        } catch (error) {
            console.warn('⚠️ [CONTACT LOOKUP] Could not resolve WhatsApp LID:', error.message);
        }
    } else {
        const jidPhone = phoneDigits(from);
        if (jidPhone) return jidPhone;
    }

    try {
        const contact = await msg.getContact();
        const contactPhone = phoneDigits(contact && contact.number);
        if (contactPhone && contactPhone !== jidDigits) return contactPhone;

        if (!isLid) {
            const contactIdPhone = phoneDigits(contact && contact.id && contact.id.user);
            if (contactIdPhone) return contactIdPhone;
        }
    } catch (error) {
        console.warn('⚠️ [CONTACT LOOKUP] Could not read WhatsApp contact:', error.message);
    }

    return isLid ? '' : phoneDigits(from);
}

module.exports = { resolveSenderNumber };
