'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const publicDir = path.join(__dirname, 'public');
const port = Number(process.env.PORT) || 3000;
const mimeTypes = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
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

const server = http.createServer(async (request, response) => {
  const method = request.method;
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
});
