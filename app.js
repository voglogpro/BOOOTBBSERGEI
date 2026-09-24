'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const publicDir = path.join(__dirname, 'public');
const dataDir = path.join(__dirname, 'data');
const port = Number(process.env.PORT) || 3000;
// Leads go to the owner's Telegram. Keep both values in the hosting environment, never in the repository.
const botToken = process.env.TELEGRAM_BOT_TOKEN || process.env.BOT_TOKEN || '';
const parseIds = value => String(value || '').split(/[\s,;]+/).filter(Boolean);
// The general chat receives every lead; a city chat receives only that city's leads.
const leadChatIds = parseIds(process.env.TELEGRAM_CHAT_ID || process.env.LEAD_CHAT_ID);

// Each city has its own address for ads: /krasnodar, /rostov.
const cities = {
  krasnodar: { name: 'Краснодар', in: 'в Краснодаре', phone: '+79181123433', chatIds: parseIds(process.env.TELEGRAM_CHAT_ID_KRASNODAR) },
  rostov: { name: 'Ростов-на-Дону', in: 'в Ростове-на-Дону', phone: '+79613237733', chatIds: parseIds(process.env.TELEGRAM_CHAT_ID_ROSTOV) }
};
const cityAliases = { 'краснодар': 'krasnodar', krd: 'krasnodar', 'ростов': 'rostov', 'ростов-на-дону': 'rostov', rnd: 'rostov', 'rostov-na-donu': 'rostov' };
const channelNames = { site: 'заявка на сайте', whatsapp: 'клиент пишет в WhatsApp', vk: 'клиент пишет ВКонтакте', telegram: 'клиент пишет в Telegram' };
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

const escapeAttr = value => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

function siteUrl(request) {
  const host = String(request.headers['x-forwarded-host'] || request.headers.host || 'localhost').split(',')[0].trim();
  const proto = String(request.headers['x-forwarded-proto'] || (host.startsWith('localhost') ? 'http' : 'https')).split(',')[0].trim();
  return `${proto}://${host}`;
}

// The page is one file; the head is filled in for the city so ads and link previews name the right city.
async function sendPage(request, response, method, cityKey) {
  const city = cities[cityKey];
  const base = siteUrl(request);
  const pageUrl = base + (city ? '/' + cityKey : '/');
  const business = {
    '@context': 'https://schema.org',
    '@type': 'LocalBusiness',
    name: 'Шоколадный фонтан — выездной шоколадный фуршет' + (city ? ' ' + city.in : ''),
    description: 'Выездной шоколадный фонтан с мастером и фруктами на свадьбы, дни рождения и корпоративы.',
    url: pageUrl,
    image: base + '/assets/og-image.jpg',
    priceRange: 'от 12 000 ₽',
    areaServed: city ? city.name : Object.values(cities).map(item => item.name),
    makesOffer: { '@type': 'Offer', name: 'Пакет «Всё включено»', price: '12000', priceCurrency: 'RUB' }
  };
  if (city) {
    business.telephone = city.phone;
    business.address = { '@type': 'PostalAddress', addressLocality: city.name, addressCountry: 'RU' };
  }
  let html = await fs.promises.readFile(path.join(publicDir, 'index.html'), 'utf8');
  html = html
    .replaceAll('%CITY_IN%', city ? city.in : 'в Краснодаре и Ростове-на-Дону')
    .replaceAll('%PAGE_URL%', escapeAttr(pageUrl))
    .replaceAll('%SITE_URL%', escapeAttr(base))
    .replace('%LD_JSON%', JSON.stringify(business).replace(/</g, '\\u003c'))
    .replace('<html lang="ru">', `<html lang="ru" data-city="${city ? cityKey : ''}">`);
  const data = Buffer.from(html);
  response.writeHead(200, {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': data.length,
    'Cache-Control': 'no-cache',
    'X-Content-Type-Options': 'nosniff'
  });
  response.end(method === 'HEAD' ? undefined : data);
}

function redirect(response, location) {
  response.writeHead(301, { Location: location, 'Cache-Control': 'no-cache' });
  response.end();
}

function sendSitemap(request, response, method) {
  const base = siteUrl(request);
  const urls = ['/', ...Object.keys(cities).map(key => '/' + key)];
  const body = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
    urls.map(url => `  <url><loc>${escapeAttr(base + url)}</loc></url>`).join('\n') + '\n</urlset>\n';
  response.writeHead(200, { 'Content-Type': 'application/xml; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  response.end(method === 'HEAD' ? undefined : body);
}

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
    ['Город', cities[lead.city]?.name],
    ['Канал', channelNames[lead.channel]],
    ['Имя', lead.name],
    ['Телефон', lead.phone],
    ['Событие', lead.event || 'уточняется'],
    ['Дата', lead.date || 'уточняется'],
    ['Время', lead.time],
    ['Гостей', lead.guests],
    ['Место', lead.place],
    ['Комментарий', lead.message],
    ['Telegram', lead.telegram]
  ].filter(([, value]) => value);
  return '🍫 <b>Новая заявка с сайта</b>\n\n' +
    rows.map(([label, value]) => `<b>${label}:</b> ${escapeHtml(value)}`).join('\n');
}

async function notifyTelegram(lead) {
  const chatIds = [...new Set([...leadChatIds, ...(cities[lead.city]?.chatIds || [])])];
  if (!botToken || !chatIds.length) return false;
  const text = formatLead(lead);
  const results = await Promise.all(chatIds.map(async chatId => {
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
    time: clean(body?.time, 10),
    guests: clean(body?.guests, 10),
    place: clean(body?.place, 120),
    city: cities[body?.city] ? body.city : '',
    channel: channelNames[body?.channel] ? body.channel : 'site',
    message: clean(body?.message, 1000),
    telegram: clean(body?.telegram, 64),
    page: clean(body?.page, 200)
  };
  // A visitor who writes from a messenger is reachable there, so the phone is optional.
  const needsPhone = lead.channel === 'site';
  if (!lead.name || (needsPhone && lead.phone.replace(/\D/g, '').length < 10)) {
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
  if (pathname === '/sitemap.xml') return sendSitemap(request, response, method);
  if (pathname === '/robots.txt') {
    return sendText(response, 200, `User-agent: *\nAllow: /\nSitemap: ${siteUrl(request)}/sitemap.xml\n`, method);
  }
  const query = request.url.includes('?') ? request.url.slice(request.url.indexOf('?')) : '';
  const slug = pathname.replace(/^\/+|\/+$/g, '').toLowerCase();
  const page = key => sendPage(request, response, method, key).catch(error => {
    console.error('Cannot render the page:', error);
    if (!response.headersSent) sendText(response, 500, 'Internal Server Error', method);
  });
  if (pathname === '/' || pathname === '/index.html') return page('');
  if (cities[slug] || cityAliases[slug]) {
    const key = cities[slug] ? slug : cityAliases[slug];
    // One canonical address per city; relative asset paths rely on no trailing slash.
    if (pathname !== '/' + key) return redirect(response, '/' + key + query);
    return page(key);
  }
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
