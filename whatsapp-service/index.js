/*
 * Academia Itaca - Servicio WhatsApp
 *
 * Funciones:
 *   1. Escucha TODOS los mensajes entrantes -> SQLite
 *   2. API REST para enviar mensajes (reemplaza pyautogui)
 *   3. API REST para consultar mensajes no procesados (para interceptor.py)
 *   4. Delegacion de herramientas de backfill y exportacion a ./tools
 *
 * Uso:
 *   node index.js              -> produccion
 *   WA_LOG_ONLY=1 node index.js -> solo log, no conecta WhatsApp (testing)
 */

const { Client, LocalAuth } = require('whatsapp-web.js');
const express = require('express');
const qrcode = require('qrcode-terminal');
const path = require('path');
const fs = require('fs');
const db = require('./db');
const backfill = require('./tools/backfill');
const exportTools = require('./tools/export');

// -- Configuracion -----------------------------------------------------
const PORT = process.env.WA_PORT || 3100;
const LOG_ONLY = process.env.WA_LOG_ONLY === '1';

// Numeros propios de la academia (ignorar mensajes enviados por nosotros)
const SELF_NUMBERS = [
    '34623927560',   // Itaca principal
    '34644365391',   // Miguel
];

// -- Resiliencia ante excepciones no controladas de Puppeteer ----------
process.on('unhandledRejection', (reason, promise) => {
    const msg = reason && reason.message ? reason.message : String(reason);
    if (msg.includes('Execution context was destroyed') || msg.includes('Target closed')) {
        console.warn('[WhatsApp Service] Advertencia de navegacion/contexto Puppeteer (ignorado):', msg);
    } else {
        console.warn('[WhatsApp Service] Unhandled Rejection:', msg);
    }
});

process.on('uncaughtException', (err) => {
    const msg = err && err.message ? err.message : String(err);
    if (msg.includes('Execution context was destroyed') || msg.includes('Target closed')) {
        console.warn('[WhatsApp Service] Advertencia de contexto Puppeteer (ignorado):', msg);
    } else {
        console.error('[WhatsApp Service] Uncaught Exception:', err);
    }
});

async function gracefulShutdown(signal) {
    console.log(`[WhatsApp Service] Recibido ${signal}, cerrando limpiamente...`);
    try {
        if (client) {
            await client.destroy();
        }
    } catch (_) {}
    process.exit(0);
}
process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

// -- Cliente WhatsApp --------------------------------------------------
let client = null;
let clientReady = false;

function initWhatsApp() {
    client = new Client({
        authStrategy: new LocalAuth({ dataPath: './wa-session' }),
        puppeteer: {
            headless: true,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disk-cache-size=52428800',
                '--media-cache-size=10485760',
            ],
        },
    });

    client.on('qr', (qr) => {
        console.log('\n╔══════════════════════════════════════╗');
        console.log('║  ESCANEA ESTE QR CON WHATSAPP WEB   ║');
        console.log('╚══════════════════════════════════════╝\n');
        qrcode.generate(qr, { small: true });
    });

    client.on('ready', () => {
        clientReady = true;
        console.log('[WhatsApp] Conectado y listo.');
    });

    client.on('authenticated', () => {
        console.log('[WhatsApp] Autenticado correctamente.');
        // Guard de sincronización: si Store está listo pero WWebJS no se ha inyectado, asegurar disparo de ready
        const syncCheck = setInterval(async () => {
            if (clientReady) {
                clearInterval(syncCheck);
                return;
            }
            try {
                const page = client?.pupPage;
                if (!page) return;
                const done = await page.evaluate(() => {
                    if (window.Store && typeof window.WWebJS === 'undefined') {
                        if (typeof window.onAppStateHasSyncedEvent === 'function') {
                            window.onAppStateHasSyncedEvent();
                            return true;
                        }
                    }
                    return false;
                });
                if (done) {
                    console.log('[WhatsApp] Sincronización completada vía guard.');
                    clearInterval(syncCheck);
                }
            } catch (_) {}
        }, 2000);
        setTimeout(() => clearInterval(syncCheck), 60000);
    });

    client.on('loading_screen', (percent, message) => {
        console.log(`[WhatsApp] Cargando: ${percent}% - ${message}`);
    });

    client.on('change_state', (state) => {
        console.log(`[WhatsApp] Estado cambiado: ${state}`);
    });

    client.on('auth_failure', (msg) => {
        console.error('[WhatsApp] Error de autenticacion:', msg);
    });

    client.on('disconnected', (reason) => {
        clientReady = false;
        console.warn('[WhatsApp] Desconectado:', reason);
        console.log('[WhatsApp] Reintentando conexion en 10s...');
        setTimeout(() => client.initialize(), 10000);
    });

    // -- Listener de mensajes entrantes --------------------------------
    client.on('message', async (msg) => {
        try {
            // Ignorar mensajes de estado/broadcast
            if (!msg || msg.from === 'status@broadcast') return;

            // Obtener info del chat de forma defensiva con múltiples fallbacks
            let chatName = msg._data?.chat?.name || msg._data?.name || '';
            let chatId = msg.from || '';
            let isGroup = Boolean(msg.from && msg.from.endsWith('@g.us'));
            try {
                const chat = await msg.getChat();
                if (chat) {
                    chatName = chat.name || chatName;
                    chatId = chat.id?._serialized || chatId;
                    if (chat.isGroup !== undefined) isGroup = Boolean(chat.isGroup);
                }
            } catch (chatErr) {
                // Si getChat falla por Error: r en WhatsApp Web, intentar extraer título formateado vía Store ligero
                if (!chatName && client?.pupPage) {
                    try {
                        const fallbackTitle = await client.pupPage.evaluate((cid) => {
                            try {
                                const wid = window.Store?.WidFactory?.createWid(cid);
                                const c = window.Store?.Chat?.get(wid);
                                return c?.formattedTitle || c?.name || '';
                            } catch (_) { return ''; }
                        }, chatId);
                        if (fallbackTitle) chatName = fallbackTitle;
                    } catch (_) {}
                }
            }

            // Fallback desde la base de datos local SQLite si sigue vacío
            if (!chatName && chatId) {
                chatName = db.getChatName(chatId);
            }

            // Obtener info del contacto de forma defensiva
            // getContact() puede lanzar 'Error: r' en WWebJS con LIDs o perfiles
            let fromName = msg._data?.notifyName || '';
            let fromNumber = '';
            try {
                const contact = await msg.getContact();
                if (contact) {
                    fromName = contact.pushname || contact.name || fromName;
                    const contactNumber = (contact.number ? String(contact.number) : '').replace(/[^\d]/g, '');
                    if (contactNumber && contactNumber.length >= 7 && contactNumber.length <= 15) {
                        fromNumber = contactNumber;
                    }
                }
            } catch (contactErr) {
                // Fallback a autor o remitente directo
            }

            if (!fromNumber) {
                const fromRaw = msg.author || msg.from || '';
                fromNumber = fromRaw.replace(/@.*$/, '');
            }

            // Ignorar mensajes enviados por la propia academia
            if (SELF_NUMBERS.includes(fromNumber)) return;

            const messageData = {
                wa_id: msg.id?._serialized || `${Date.now()}_${fromNumber}`,
                timestamp: msg.timestamp || Math.floor(Date.now() / 1000),
                from_number: fromNumber,
                from_name: fromName,
                chat_name: chatName,
                chat_id: chatId,
                body: msg.body || '',
                is_group: isGroup ? 1 : 0,
                has_media: msg.hasMedia ? 1 : 0,
            };

            db.saveMessage(messageData);

            const src = messageData.is_group ? `[G:${messageData.chat_name}]` : '[DM]';
            console.log(`${src} ${messageData.from_name || fromNumber}: ${(messageData.body || '').substring(0, 80)}`);

        } catch (err) {
            console.error('[WhatsApp] Error procesando mensaje:', err?.stack || err?.message || err);
        }
    });

    // Capturar mensajes salientes (para contexto)
    client.on('message_create', async (msg) => {
        try {
            if (!msg || !msg.fromMe) return;
            if (msg.from === 'status@broadcast') return;

            let chatName = msg._data?.chat?.name || msg._data?.name || '';
            let chatId = msg.to || msg.from || '';
            let isGroup = Boolean(chatId && chatId.endsWith('@g.us'));
            try {
                const chat = await msg.getChat();
                if (chat) {
                    chatName = chat.name || chatName;
                    chatId = chat.id?._serialized || chatId;
                    if (chat.isGroup !== undefined) isGroup = Boolean(chat.isGroup);
                }
            } catch (chatErr) {
                if (!chatName && client?.pupPage) {
                    try {
                        const fallbackTitle = await client.pupPage.evaluate((cid) => {
                            try {
                                const wid = window.Store?.WidFactory?.createWid(cid);
                                const c = window.Store?.Chat?.get(wid);
                                return c?.formattedTitle || c?.name || '';
                            } catch (_) { return ''; }
                        }, chatId);
                        if (fallbackTitle) chatName = fallbackTitle;
                    } catch (_) {}
                }
            }

            if (!chatName && chatId) {
                chatName = db.getChatName(chatId);
            }

            const messageData = {
                wa_id: msg.id?._serialized || `${Date.now()}_self`,
                timestamp: msg.timestamp || Math.floor(Date.now() / 1000),
                from_number: 'ACADEMIA',
                from_name: 'Academia Itaca',
                chat_name: chatName,
                chat_id: chatId,
                body: msg.body || '',
                is_group: isGroup ? 1 : 0,
                has_media: msg.hasMedia ? 1 : 0,
                is_system: 1,
                processed: 1,
            };

            db.saveMessage(messageData);

        } catch (err) {
            // Silencioso para mensajes propios
        }
    });

    console.log('[WhatsApp] Inicializando cliente...');
    client.initialize();
}

// -- API REST ----------------------------------------------------------
const app = express();
app.use(express.json());

// Health check
app.get('/api/health', (req, res) => {
    res.json({
        status: clientReady ? 'connected' : 'disconnected',
        stats: db.getStats(),
    });
});

// Debug status
app.get('/api/debug', async (req, res) => {
    try {
        const page = client?.pupPage;
        if (!page) return res.json({ error: 'no pupPage' });
        const url = page.url();
        const state = await client.getState().catch(e => String(e));
        const storeExists = await page.evaluate(() => typeof window.Store !== 'undefined').catch(e => String(e));
        const wwebExists = await page.evaluate(() => typeof window.WWebJS !== 'undefined').catch(e => String(e));
        const hasSynced = await page.evaluate(() => Boolean(window.AuthStore?.AppState?.hasSynced)).catch(e => String(e));
        const appState = await page.evaluate(() => window.AuthStore?.AppState?.state || null).catch(e => String(e));

        let syncError = null;
        if (req.query.triggerSync === '1') {
            syncError = await page.evaluate(async () => {
                try {
                    if (typeof window.onAppStateHasSyncedEvent === 'function') {
                        await window.onAppStateHasSyncedEvent();
                        return 'OK';
                    }
                    return 'NO_FN';
                } catch (e) {
                    return e?.stack || e?.message || String(e);
                }
            }).catch(e => String(e));
        }

        res.json({ url, state, storeExists, wwebExists, hasSynced, appState, clientReady, syncError });
    } catch (err) {
        res.json({ error: err.message });
    }
});

// -- Enviar mensaje ----------------------------------------------------
app.post('/api/send', async (req, res) => {
    const { phone, message } = req.body;

    if (!phone || !message) {
        return res.status(400).json({ success: false, error: 'Faltan phone o message' });
    }

    if (!clientReady) {
        db.logSend(phone, message, false, 'Cliente WhatsApp no conectado');
        return res.status(503).json({ success: false, error: 'WhatsApp no conectado' });
    }

    try {
        let chatId;
        if (phone.includes('@')) {
            chatId = phone;
        } else {
            const cleanPhone = phone.replace(/[\s\-\+]/g, '');
            chatId = `${cleanPhone}@c.us`;
        }

        const sent = await client.sendMessage(chatId, message);

        try {
            const cleanPhone = phone.replace(/[\s\-\+]/g, '');
            db.saveMessage({
                wa_id: sent?.id?._serialized || `${Date.now()}_api_${cleanPhone}`,
                timestamp: sent?.timestamp || Math.floor(Date.now() / 1000),
                from_number: 'ACADEMIA',
                from_name: 'Academia Itaca',
                chat_name: '',
                chat_id: chatId,
                body: message,
                is_group: false,
                has_media: false,
                is_system: true,
                processed: true,
            });
        } catch (e) {
            console.error('[API] No pude marcar mensaje saliente como is_system:', e.message);
        }

        db.logSend(phone, message, true, null);
        res.json({ success: true });

    } catch (err) {
        db.logSend(phone, message, false, err.message);
        res.status(500).json({ success: false, error: err.message });
    }
});

// -- Obtener mensajes no procesados (legacy) --------------------------
app.get('/api/messages', (req, res) => {
    const limit = parseInt(req.query.limit) || 500;
    const messages = db.getUnprocessedMessages(limit);
    res.json({ count: messages.length, messages });
});

// -- Chats con conversaciones "frias" (listas para procesar) ----------
app.get('/api/chats/cooled', (req, res) => {
    const cooldown = req.query.cooldown !== undefined ? parseInt(req.query.cooldown) : 10800;
    const chats = db.getCooledChats(cooldown);
    res.json({ count: chats.length, chats });
});

// -- Hilo completo de un chat (ultima semana) -------------------------
app.get('/api/chats/:chatId/thread', (req, res) => {
    const chatId = decodeURIComponent(req.params.chatId);
    const days = parseInt(req.query.days) || 7;
    const thread = db.getChatThread(chatId, days);
    const unprocessedIds = db.getUnprocessedIdsForChat(chatId);
    res.json({ chat_id: chatId, total: thread.length, unprocessed_ids: unprocessedIds, messages: thread });
});

// -- Marcar mensajes como procesados ----------------------------------
app.post('/api/messages/processed', (req, res) => {
    const { ids, category, llm_response } = req.body;

    if (!ids || !Array.isArray(ids)) {
        return res.status(400).json({ success: false, error: 'ids debe ser un array' });
    }

    try {
        db.markProcessed(ids, category, llm_response);
        res.json({ success: true, processed: ids.length });
    } catch (err) {
        res.status(500).json({ success: false, error: err.message });
    }
});

// -- Marcar un mensaje individual -------------------------------------
app.put('/api/messages/:id/processed', (req, res) => {
    const { id } = req.params;
    const { category, llm_response } = req.body || {};

    try {
        db.markOneProcessed(parseInt(id), category, llm_response);
        res.json({ success: true });
    } catch (err) {
        res.status(500).json({ success: false, error: err.message });
    }
});

// -- Estadisticas -----------------------------------------------------
app.get('/api/stats', (req, res) => {
    res.json(db.getStats());
});

// ══════════════════════════════════════════════════════════════════════
// BACKFILL Y EXPORTACION (delegado a ./tools)
// ══════════════════════════════════════════════════════════════════════

app.post('/api/backfill/start', async (req, res) => {
    if (!clientReady) {
        return res.status(503).json({ success: false, error: 'WhatsApp no conectado' });
    }
    if (backfill.isAnyBackfillRunning()) {
        return res.status(409).json({
            success: false,
            error: 'Backfill ya en curso',
            progress: backfill.getBackfillStatus(),
        });
    }

    const limit = parseInt(req.body?.limit) || 500;
    res.json({
        success: true,
        message: `Backfill iniciado (limit=${limit} msgs/chat). Consulta GET /api/backfill/status.`,
    });

    backfill.runBackfill(client, limit, SELF_NUMBERS).catch(err => {
        console.error('[Backfill] Error fatal:', err.message);
    });
});

app.get('/api/backfill/status', (req, res) => {
    res.json(backfill.getBackfillStatus());
});

app.post('/api/backfill/deep', async (req, res) => {
    if (!clientReady) {
        return res.status(503).json({ success: false, error: 'WhatsApp no conectado' });
    }
    if (backfill.isAnyBackfillRunning()) {
        return res.status(409).json({
            success: false,
            error: 'Ya hay un backfill en curso',
            deepState: backfill.getDeepStatus(),
            backfillState: backfill.getBackfillStatus(),
        });
    }

    res.json({
        success: true,
        message: 'Deep backfill (searchMessages) iniciado. Consulta GET /api/backfill/deep/status. Tarda ~20-40 min.',
    });

    backfill.runDeepBackfill(client, SELF_NUMBERS).catch(err => {
        console.error('[DeepBackfill] Error fatal:', err.message);
    });
});

app.get('/api/backfill/deep/status', (req, res) => {
    res.json(backfill.getDeepStatus());
});

app.get('/api/debug/store', (req, res) => {
    res.status(410).json({
        success: false,
        error: 'Endpoint eliminado. Usa /api/backfill/deep para rescatar historial.',
    });
});

app.get('/api/export/chats', async (req, res) => {
    if (!clientReady) {
        return res.status(503).json({ success: false, error: 'WhatsApp no conectado' });
    }
    try {
        const chats = await exportTools.getExportChats(client);
        res.json({ count: chats.length, chats });
    } catch (err) {
        res.status(500).json({ success: false, error: err.message });
    }
});

app.post('/api/export/all', async (req, res) => {
    if (!clientReady) {
        return res.status(503).json({ success: false, error: 'WhatsApp no conectado' });
    }
    res.json({ success: true, message: 'Exportacion iniciada. Ver progreso en logs.' });
    exportTools.runExportAll(client, __dirname).catch(err => {
        console.error(`[Export] Error general: ${err.message}`);
    });
});

// -- Arranque ----------------------------------------------------------
app.listen(PORT, () => {
    console.log(`[API] Escuchando en puerto ${PORT}`);
    console.log(`[API] Endpoints:`);
    console.log(`  GET  /api/health`);
    console.log(`  POST /api/send              {phone, message}`);
    console.log(`  GET  /api/messages?limit=N`);
    console.log(`  POST /api/messages/processed {ids, category, llm_response}`);
    console.log(`  PUT  /api/messages/:id/processed`);
    console.log(`  GET  /api/stats`);

    if (LOG_ONLY) {
        console.log('\n[MODO] LOG_ONLY - WhatsApp NO se conectara');
    } else {
        initWhatsApp();
    }
});
