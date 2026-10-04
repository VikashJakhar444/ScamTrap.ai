/**
 * ScamTrap AI - Real WhatsApp Device Bridge (whatsapp-web.js)
 * 100% Real 2-Phone Architecture:
 * - Phone 1 (Victim Phone): Linked via QR Code in Terminal & Dashboard
 * - Phone 2 (Scammer Phone): Sends incoming scam messages to Phone 1
 */

const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
const { resolveSenderNumber } = require('./sender_identity');

// Minimal .env loader (keeps the bridge dependency-free for config).
(function loadEnvFile() {
    const envPath = path.join(process.cwd(), '.env');
    try {
        if (!fs.existsSync(envPath)) return;
        fs.readFileSync(envPath, 'utf8').split(/\r?\n/).forEach((line) => {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('#')) return;
            const eq = trimmed.indexOf('=');
            if (eq === -1) return;
            const key = trimmed.slice(0, eq).trim();
            let value = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
            if (key && !(key in process.env)) process.env[key] = value;
        });
    } catch (e) {
        // .env is optional
    }
})();

const BACKEND_BASE = process.env.BACKEND_BASE || 'http://localhost:8000';
const API_KEY = (process.env.SCAMTRAP_API_KEY || '').trim();

// Every backend call carries the API key so the bridge keeps working when the
// backend is reached over a tunnel instead of plain loopback.
const http = axios.create({
    baseURL: BACKEND_BASE,
    headers: API_KEY ? { 'X-API-Key': API_KEY } : {}
});
http.interceptors.response.use(
    (res) => res,
    (err) => {
        if (err.response && err.response.status === 401) {
            console.error('🔒 [AUTH ERROR] Backend rejected the bridge (401).');
            console.error('   Set the same SCAMTRAP_API_KEY in .env for app.py and wa_bridge.js.');
        }
        return Promise.reject(err);
    }
);

let lastIncomingScammerNumber = '';
let isConnected = false;
let myJid = null;

// Gracefully handle transient Puppeteer navigation errors without crashing
process.on('uncaughtException', (err) => {
    if (err.message && err.message.includes('Execution context was destroyed')) {
        console.log('🔄 [RECOVERY] Puppeteer page context refreshed safely.');
    } else {
        console.error('⚠️ [PROCESS WARNING]:', err.message);
    }
});

process.on('unhandledRejection', (reason) => {
    console.warn('⚠️ [ASYNC PROMISE WARNING]:', reason);
});

console.log('================================================================');
console.log('🛡️  SCAMTRAP AI: REAL 2-PHONE WHATSAPP WEB BRIDGE');
console.log('🔗 Backend Base:', BACKEND_BASE);
console.log('================================================================');

const client = new Client({
    authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
    webVersionCache: {
        type: 'remote',
        remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-js/main/dist/wppconnect-wa.js'
    },
    puppeteer: {
        headless: true,
        executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--disable-gpu'
        ]
    }
});

client.on('loading_screen', (percent, message) => {
    console.log(`⏳ [SYNCING WHATSAPP]: ${percent}% - ${message}`);
});

// 1. QR Code Generation
client.on('qr', async (qr) => {
    console.log('\n📲 [QR CODE GENERATED] Scan with Phone 1 (WhatsApp -> Linked Devices -> Link a Device):\n');
    qrcode.generate(qr, { small: true });
    console.log('👉 You can also scan the QR directly from the SOC Dashboard at http://localhost:8000\n');

    try {
        await http.post('/api/wa-status', {
            status: 'QR_READY',
            qr: qr
        }, { timeout: 3000 });
    } catch (e) {
        // backend might be restarting
    }
});

// 2. Authentication State
client.on('authenticated', async () => {
    console.log('🔐 [AUTHENTICATED] WhatsApp session verified.');
    try {
        await http.post('/api/wa-status', {
            status: 'AUTHENTICATED'
        }, { timeout: 3000 });
    } catch (e) { }
});

client.on('auth_failure', (msg) => {
    console.error('❌ [AUTH FAILURE]:', msg);
});

// 3. Ready State
client.on('ready', async () => {
    isConnected = true;
    const phone = client.info && client.info.wid ? client.info.wid.user : 'Linked Device';
    myJid = client.info && client.info.wid ? client.info.wid._serialized : null;

    console.log('\n================================================================');
    console.log(`🚀 [WHATSAPP READY] Phone 1 Connected: +${phone}`);
    console.log(`📱 Linked JID: ${myJid}`);
    console.log('⚡ Honeypot is actively listening for incoming messages.');
    console.log('================================================================\n');

    try {
        await http.post('/api/wa-status', {
            status: 'CONNECTED',
            phone: phone,
            my_jid: myJid
        }, { timeout: 3000 });
    } catch (e) { }
});

client.on('disconnected', async (reason) => {
    console.log('⚠️ [DISCONNECTED]:', reason);
    isConnected = false;
    try {
        await http.post('/api/wa-status', {
            status: 'DISCONNECTED',
            reason: reason
        }, { timeout: 3000 });
    } catch (e) { }
});

// 4. Helper: Send Bot Live Report into Phone 1 "Message Yourself"
async function sendBotReportToSelf(reportText) {
    if (!reportText) return;
    const selfJid = client.info && client.info.wid ? client.info.wid._serialized : myJid;
    if (!selfJid) return;
    try {
        await client.sendMessage(selfJid, reportText);
        console.log(`📢 [LIVE REPORT POSTED TO PHONE 1]: ${reportText.slice(0, 80)}...`);
    } catch (err) {
        console.error('⚠️ Could not post report to self-chat:', err.message);
    }
}

// 5. Direct Bot Command in "Message Yourself" or Command from Phone 1
client.on('message_create', async (msg) => {
    try {
        if (!msg.fromMe) return;

        const text = (msg.body || '').trim();
        if (!text) return;

        // Ignore bot's own automated output prefixes to prevent infinite loops
        const IGNORE_PREFIXES = ['🫡', '✅', '🎯', '🛡️', '⚠️', '🚨', '[LIVE UPDATE]', '🔴', '🤖', '👁️', '📊', '📋', 'ℹ️'];
        if (IGNORE_PREFIXES.some(p => text.startsWith(p))) {
            return;
        }

        const selfJid = client.info && client.info.wid ? client.info.wid._serialized : (myJid || msg.from);
        const myUser = client.info && client.info.wid ? client.info.wid.user : '';
        const isSelfChat = msg.to === msg.from || msg.to === selfJid || (myUser && (msg.to.includes(myUser) || msg.from.includes(myUser)));

        const lower = text.toLowerCase();
        const isCommand = lower.startsWith('trap') || lower.startsWith('monitor') || lower.startsWith('watch') ||
            lower === 'status' || lower === 'help' || lower === 'reset' ||
            lower.startsWith('stop') || lower.startsWith('un-trap') || lower.startsWith('standby');

        if (!isSelfChat && !isCommand) return;

        console.log(`\n💬 [COMMAND DETECTED ON PHONE 1]: "${text}" (to: ${msg.to})`);

        // Forward self command to backend
        const res = await http.post('/api/bot-self-command', {
            text: text,
            last_incoming_from: lastIncomingScammerNumber
        }, { timeout: 15000 });

        const data = res.data || {};

        // 1. Send bot confirmation to user in self-chat
        if (data.bot_confirm_msg) {
            await client.sendMessage(selfJid, data.bot_confirm_msg);
            console.log(`🤖 [BOT CONFIRMATION SENT TO USER]: ${data.bot_confirm_msg.slice(0, 80)}...`);
        }

        // 2. If target scammer was identified in TRAP mode, send initial bait reply directly to scammer
        if (data.target_scammer_jid && data.scammer_reply_text) {
            console.log(`🎯 [HIJACKING SCAMMER ${data.target_scammer_jid}]: "${data.scammer_reply_text}"`);
            let chat = null;
            try { chat = await client.getChatById(data.target_scammer_jid); } catch (e) { }
            await simulateHumanReply(chat, data.target_scammer_jid, data.scammer_reply_text, data.scammer_media_base64, data.scammer_delivery_plan);
        }
    } catch (err) {
        console.error('❌ Error handling self-command:', err.message);
    }
});

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// WhatsApp typing state expires after a few seconds - refresh it for long
// typing runs so the indicator genuinely stays on while the "human types".
async function holdTyping(chat, ms) {
    try { if (chat && chat.sendStateTyping) await chat.sendStateTyping(); } catch (e) { }
    const end = Date.now() + ms;
    while (Date.now() < end - 250) {
        const chunk = Math.min(6000, end - Date.now());
        if (chunk <= 0) break;
        await sleep(chunk);
        if (Date.now() < end - 800) {
            try { if (chat && chat.sendStateTyping) await chat.sendStateTyping(); } catch (e) { }
        }
    }
}

/**
 * Replays the backend's human delivery plan step by step.
 * Plan steps: {action:'wait'|'typing'|'clear_state'|'send'|'send_media', ms/text/caption}
 *
 * This is what makes the honeypot indistinguishable from a person:
 *  - read/pickup delay before typing even starts (never instant),
 *  - typing speed proportional to message length with pauses,
 *  - multiple short bubbles instead of one wall of text,
 *  - the app-switch + screenshot narrative for payment-failure images.
 */
async function executeDeliveryPlan(chat, jid, plan, mediaBase64 = null) {
    for (const step of plan) {
        switch (step.action) {
            case 'wait':
                console.log(`   ⏳ wait ${(step.ms / 1000).toFixed(1)}s`);
                await sleep(step.ms);
                break;
            case 'typing':
                console.log(`   ⌨️ typing ${(step.ms / 1000).toFixed(1)}s...`);
                await holdTyping(chat, step.ms);
                break;
            case 'clear_state':
                try { if (chat && chat.clearState) await chat.clearState(); } catch (e) { }
                break;
            case 'send':
                console.log(`   ➡️ send: "${(step.text || '').slice(0, 70)}"`);
                await client.sendMessage(jid, step.text);
                break;
            case 'send_media':
                console.log(`   📎 send screenshot + caption`);
                if (mediaBase64) {
                    const media = new MessageMedia('image/png', mediaBase64, 'phonepe_failed_receipt.png');
                    await client.sendMessage(jid, media, { caption: step.caption || '' });
                } else if (step.caption) {
                    await client.sendMessage(jid, step.caption);
                }
                break;
        }
    }
    console.log(`✅ [DELIVERY COMPLETE] to ${jid}`);
}

/**
 * Sends a reply like a human would.
 * Preferred path: a delivery_plan from the backend (accurate typing model).
 * Legacy path: local word-count timing, used only if no plan was provided.
 */
async function simulateHumanReply(chat, jid, text, mediaBase64 = null, plan = null) {
    if (Array.isArray(plan) && plan.length > 0) {
        console.log(`\n🧑 [HUMAN DELIVERY PLAN] ${plan.length} steps for ${jid}`);
        await executeDeliveryPlan(chat, jid, plan, mediaBase64);
        return;
    }

    // ---- Legacy fallback timing (no plan supplied by backend) ----
    if (mediaBase64) {
        console.log(`\n📱 [SCREENSHOT SIMULATION] app switch + failed txn...`);
        await sleep(Math.floor(Math.random() * 6000) + 20000);
        await sleep(Math.floor(Math.random() * 2000) + 3000);
        await holdTyping(chat, Math.floor(Math.random() * 8000) + 24000);
        const media = new MessageMedia('image/png', mediaBase64, 'phonepe_failed_receipt.png');
        await client.sendMessage(jid, media, { caption: text || '' });
        console.log(`✅ [PAYMENT BAIT SENT]`);
    } else {
        const words = (text || '').trim().split(/\s+/).length;
        const delay = words <= 4 ? (Math.random() * 2500 + 5500) : (words <= 10 ? (Math.random() * 4000 + 8000) : (Math.random() * 5000 + 13000));
        console.log(`⏳ [LEGACY TYPING]: ${words} words -> ${(delay / 1000).toFixed(1)}s`);
        await holdTyping(chat, delay);
        await client.sendMessage(jid, text);
        console.log(`✅ [TEXT REPLY SENT]`);
    }
}

const seenMsgIds = new Set();

// 6. Incoming Messages from Phone 2 (or any other number) to Phone 1
client.on('message', async (msg) => {
    try {
        // Ignore status broadcasts and group messages
        if (msg.from === 'status@broadcast' || msg.from.endsWith('@g.us')) {
            return;
        }

        const msgId = msg.id ? msg.id._serialized : null;
        if (msgId && seenMsgIds.has(msgId)) {
            return;
        }
        if (msgId) {
            seenMsgIds.add(msgId);
            // FIFO eviction: dropping the oldest entry keeps dedup intact.
            // Clearing the whole set would re-admit old IDs and double-reply.
            while (seenMsgIds.size > 2000) {
                seenMsgIds.delete(seenMsgIds.values().next().value);
            }
        }

        const text = (msg.body || '').trim();
        if (!text) return;

        const senderNumber = await resolveSenderNumber(msg, client);
        lastIncomingScammerNumber = senderNumber;
        if (!senderNumber && msg.from.endsWith('@lid')) {
            console.warn(`⚠️ [CONTACT LOOKUP] No phone number is available for WhatsApp LID ${msg.from}; it will not be reported as a phone.`);
        }

        console.log(`\n📩 [INCOMING WHATSAPP MESSAGE] From: ${senderNumber ? `+${senderNumber}` : msg.from} -> "${text}"`);

        // Forward to backend
        const res = await http.post('/api/wa-incoming', {
            msg_id: msgId,
            sender_jid: msg.from,
            sender_number: senderNumber,
            text: text
        }, { timeout: 25000 });

        const data = res.data || {};

        if (data.status === 'STANDBY') {
            console.log(`⏳ [STANDBY] Target +${senderNumber} on standby. Message logged to dashboard. User can type 'trap' or 'monitor' to engage.`);
            return;
        }

        let chat = null;
        try { chat = await msg.getChat(); } catch (e) { }

        if (data.status === 'MONITORED') {
            if (data.should_reply && data.reply_text) {
                console.log(`🧠 [MONITOR STALL REPLY] Payment ask detected. Sending delay response to +${senderNumber} -> "${data.reply_text}"`);
                await simulateHumanReply(chat, msg.from, data.reply_text, null, data.delivery_plan);
            } else {
                console.log(`👁️ [MONITOR ONLY] Target +${senderNumber} message intercepted. Passive intel logged without auto-reply.`);
            }

            if (data.bot_report) {
                await sendBotReportToSelf(data.bot_report);
            }
            return;
        }

        if (data.status === 'TRAPPED' && data.should_reply) {
            console.log(`🧠 [AI HIJACK AGENT REPLY] To: +${senderNumber} -> "${data.reply_text}"`);
            await simulateHumanReply(chat, msg.from, data.reply_text, data.media_base64, data.delivery_plan);

            // Send live report to Phone 1 self-chat
            if (data.bot_report) {
                await sendBotReportToSelf(data.bot_report);
            }
        }
    } catch (err) {
        console.error('❌ Error in incoming message handler:', err.message);
    }
});

// 7. Polling Outbox Queue (Triggered when user clicks "Lock & Trap" on Dashboard or Canary IP hit)
async function pollOutboxQueue() {
    if (!isConnected) return;
    try {
        const res = await http.get('/api/wa-outbox', { timeout: 3000 });
        const items = res.data && res.data.queue ? res.data.queue : [];

        for (const item of items) {
            if (item.target_jid && (item.text || item.media_base64)) {
                console.log(`📤 [DISPATCHING QUEUED OUTBOX TO ${item.target_jid}]: "${item.text || ''}"`);
                if (item.media_base64) {
                    const media = new MessageMedia('image/png', item.media_base64, 'upi_failed.png');
                    await client.sendMessage(item.target_jid, media, { caption: item.text || '' });
                } else if (item.text) {
                    await client.sendMessage(item.target_jid, item.text);
                }
            }

            if (item.bot_report) {
                await sendBotReportToSelf(item.bot_report);
            }
        }
    } catch (e) {
        // Backend poll idle
    }
}

setInterval(pollOutboxQueue, 1500);

// Initialize WhatsApp Web Client
client.initialize();
