'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const publicDir = path.join(__dirname, 'public');
const dataDir = path.join(__dirname, 'data');
const port = Number(process.env.PORT) || 3000;
// Leads go to the owner's Telegram. Keep both values in the hosting environment, never in the repository.
const botToken = process.env.TELEGRAM_BOT_TOKEN || process.env.BOT_TOKEN || '';
const leadChatIds = String(process.env.TELEGRAM_CHAT_ID || process.env.LEAD_CHAT_ID || '')
  .split(/[\s,;]+/)
  .filter(Boolean);
const mimeTypes = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.xml': 'application/xml; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mp4': 'video/mp4',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp'
};

function sendText(response, status, body, method) {
  const data = Buffer.from(body);
  response.writeHead(status, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Content-Length': data.length,
    'X-Content-Type-Options': 'nosniff'
  });
  response.end(method === 'HEAD' ? undefined : data);
}

function sendJson(response, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': data.length,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff'
  });
  response.end(data);
}

function readBody(request, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on('data', chunk => {
      size += chunk.length;
      if (size > limit) {
        // Keep draining so the client still receives the 413 reply.
        reject(Object.assign(new Error('Payload too large'), { status: 413 }));
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    request.on('error', reject);
  });
}

// Counts accepted leads only, so a visitor fixing a typo is never locked out.
const recentLeads = new Map();
const leadWindowMs = 10 * 60 * 1000;
function leadTimes(ip) {
  const now = Date.now();
  return (recentLeads.get(ip) || []).filter(time => now - time < leadWindowMs);
}
function isRateLimited(ip) {
  return leadTimes(ip).length >= 8;
}
function rememberLead(ip) {
  if (recentLeads.size > 5000) recentLeads.clear();
  recentLeads.set(ip, [...leadTimes(ip), Date.now()]);
}

const clean = (value, max) => String(value ?? '').replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '').trim().slice(0, max);
const escapeHtml = value => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function formatLead(lead) {
  const rows = [
    ['Имя', lead.name],
    ['Телефон', lead.phone],
    ['Дата', lead.date || 'уточняется'],
    ['Событие', lead.event || 'уточняется'],
    ['Гостей', lead.guests || 'уточняется'],
    ['Комментарий', lead.message],
    ['Telegram', lead.telegram]
  ].filter(([, value]) => value);
  return '🍫 <b>Новая заявка с сайта</b>\n\n' +
    rows.map(([label, value]) => `<b>${label}:</b> ${escapeHtml(value)}`).join('\n');
}

async function notifyTelegram(lead) {
  if (!botToken || !leadChatIds.length) return false;
  const text = formatLead(lead);
  const results = await Promise.all(leadChatIds.map(async chatId => {
    try {
      const reply = await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: chatId, text, parse_mode: 'HTML', disable_web_page_preview: true }),
        signal: AbortSignal.timeout(8000)
      });
      if (!reply.ok) console.error('Telegram rejected the lead:', reply.status, await reply.text());
      return reply.ok;
    } catch (error) {
      console.error('Cannot reach Telegram:', error.message);
      return false;
    }
  }));
  return results.some(Boolean);
}

async function handleLead(request, response) {
  const ip = String(request.headers['x-forwarded-for'] || request.socket.remoteAddress || '').split(',')[0].trim();
  if (isRateLimited(ip)) return sendJson(response, 429, { ok: false, error: 'rate_limited' });

  let body;
  try {
    body = JSON.parse(await readBody(request, 16 * 1024));
  } catch (error) {
    return sendJson(response, error.status || 400, { ok: false, error: 'bad_request' });
  }
  // Bots fill every field, people never see this one.
  if (body && body.company) return sendJson(response, 200, { ok: true, delivered: true });

  const lead = {
    name: clean(body?.name, 80),
    phone: clean(body?.phone, 40),
    date: clean(body?.date, 20),
    event: clean(body?.event, 60),
    guests: clean(body?.guests, 10),
    message: clean(body?.message, 1000),
    telegram: clean(body?.telegram, 64),
    page: clean(body?.page, 200)
  };
  if (!lead.name || lead.phone.replace(/\D/g, '').length < 10) {
    return sendJson(response, 422, { ok: false, error: 'invalid' });
  }

  rememberLead(ip);
  const record = JSON.stringify({ receivedAt: new Date().toISOString(), ...lead }) + '\n';
  try {
    await fs.promises.mkdir(dataDir, { recursive: true });
    await fs.promises.appendFile(path.join(dataDir, 'leads.jsonl'), record);
  } catch (error) {
    console.error('Cannot store the lead:', error.message);
  }
  console.log('New lead:', record.trim());

  const delivered = await notifyTelegram(lead);
  return sendJson(response, 200, { ok: true, delivered });
}

// Helps the owner find the chat id: write /start to the bot, restart the app, read the log.
async function logRecentChats() {
  if (!botToken || leadChatIds.length) return;
  try {
    const reply = await fetch(`https://api.telegram.org/bot${botToken}/getUpdates`, { signal: AbortSignal.timeout(8000) });
    const data = await reply.json();
    const chats = new Map();
    for (const update of data.result || []) {
      const chat = (update.message || update.my_chat_member || {}).chat;
      if (chat) chats.set(chat.id, [chat.first_name, chat.last_name, chat.title, chat.username && '@' + chat.username].filter(Boolean).join(' '));
    }
    if (chats.size) {
      console.log('Заявки пока некуда отправлять. Укажите TELEGRAM_CHAT_ID. Чаты, писавшие боту:');
      chats.forEach((name, id) => console.log(`  ${id} — ${name}`));
    } else {
      console.log('Заявки пока некуда отправлять: напишите боту /start, перезапустите приложение и найдите свой chat id в логе.');
    }
  } catch (error) {
    console.error('Cannot read bot updates:', error.message);
  }
}

const server = http.createServer(async (request, response) => {
  const method = request.method;
  if (request.url.split('?')[0] === '/api/lead') {
    if (method !== 'POST') {
      response.setHeader('Allow', 'POST');
      return sendJson(response, 405, { ok: false, error: 'method_not_allowed' });
    }
    return handleLead(request, response).catch(error => {
      console.error('Lead handling failed:', error);
      if (!response.headersSent) sendJson(response, 500, { ok: false, error: 'server_error' });
    });
  }
  if (method !== 'GET' && method !== 'HEAD') {
    response.setHeader('Allow', 'GET, HEAD');
    return sendText(response, 405, 'Method Not Allowed', method);
  }

  let pathname;
  try {
    pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
  } catch {
    return sendText(response, 400, 'Bad Request', method);
  }

  if (pathname === '/health') return sendText(response, 200, 'ok', method);
  if (pathname.includes('\0') || pathname.includes('\\')) {
    return sendText(response, 400, 'Bad Request', method);
  }

  const relativePath = pathname === '/' ? 'index.html' : pathname.slice(1);
  const filePath = path.resolve(publicDir, relativePath);
  if (!filePath.startsWith(publicDir + path.sep)) {
    return sendText(response, 403, 'Forbidden', method);
  }

  let stats;
  try {
    stats = await fs.promises.stat(filePath);
    if (!stats.isFile()) return sendText(response, 404, 'Not Found', method);
  } catch (error) {
    if (error.code === 'ENOENT' || error.code === 'ENOTDIR') {
      return sendText(response, 404, 'Not Found', method);
    }
    console.error('Cannot read file:', error);
    return sendText(response, 500, 'Internal Server Error', method);
  }

  const headers = {
    'Content-Type': mimeTypes[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
    'Accept-Ranges': 'bytes',
    'Cache-Control': /\.(?:html|css|js)$/.test(relativePath) ? 'no-cache' : 'public, max-age=3600',
    'X-Content-Type-Options': 'nosniff'
  };

  let start = 0;
  let end = stats.size - 1;
  let status = 200;
  const range = request.headers.range;
  if (range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (!match || (!match[1] && !match[2])) {
      response.writeHead(416, { 'Content-Range': `bytes */${stats.size}` });
      return response.end();
    }
    if (match[1]) {
      start = Number(match[1]);
      if (match[2]) end = Number(match[2]);
    } else {
      start = Math.max(0, stats.size - Number(match[2]));
    }
    end = Math.min(end, stats.size - 1);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end || start >= stats.size) {
      response.writeHead(416, { 'Content-Range': `bytes */${stats.size}` });
      return response.end();
    }
    headers['Content-Range'] = `bytes ${start}-${end}/${stats.size}`;
    status = 206;
  }

  headers['Content-Length'] = end - start + 1;
  response.writeHead(status, headers);
  if (method === 'HEAD') return response.end();
  const stream = fs.createReadStream(filePath, { start, end });
  stream.on('error', error => {
    console.error('Cannot stream file:', error);
    response.destroy(error);
  });
  stream.pipe(response);
});

server.listen(port, '0.0.0.0', () => {
  console.log(`Chocolate fountain site listening on port ${port}`);
  if (!botToken) console.log('TELEGRAM_BOT_TOKEN не задан: заявки сохраняются только в data/leads.jsonl и в логе.');
  logRecentChats();
});
