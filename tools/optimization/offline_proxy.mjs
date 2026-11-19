import http from 'node:http';
import net from 'node:net';

export async function startOfflineProxy(baseUrl) {
  const base = new URL(baseUrl);
  if (base.protocol !== 'http:' || !['127.0.0.1', '[::1]', 'localhost'].includes(base.hostname)) {
    throw new TypeError('fixture origin must be loopback HTTP');
  }
  const allowed = base.origin;
  const tunnels = new Set();
  const server = http.createServer((req, res) => {
    let target;
    try { target = new URL(req.url, allowed); }
    catch { res.writeHead(400).end(); return; }
    if (target.origin !== allowed || target.username || target.password) {
      res.writeHead(403).end('external target blocked');
      return;
    }
    const forward = http.request(target, {
      method: req.method, headers: { ...req.headers, host: target.host },
    }, upstream => {
      res.writeHead(upstream.statusCode, upstream.headers);
      upstream.on('error', () => res.destroy());
      upstream.pipe(res);
    });
    forward.on('error', () => {
      if (!res.headersSent) res.writeHead(502);
      res.end('fixture upstream unavailable');
    });
    req.on('aborted', () => forward.destroy());
    req.on('error', () => forward.destroy());
    res.on('error', () => forward.destroy());
    res.on('close', () => { if (!res.writableFinished) forward.destroy(); });
    req.pipe(forward);
  });
  // Chrome closes rejected/background connections, sometimes with an RST.
  // Register before the CONNECT allowlist can return so resets cannot escape.
  server.on('connection', socket => socket.on('error', () => socket.destroy()));
  server.on('connect', (req, socket, head) => {
    if (req.url !== base.host) {
      socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');
      return;
    }
    const upstream = net.connect(Number(base.port || 80), base.hostname, () => {
      socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      socket.pipe(upstream);
      upstream.pipe(socket);
    });
    tunnels.add(socket);
    socket.on('close', () => { tunnels.delete(socket); upstream.destroy(); });
    upstream.on('error', () => socket.destroy());
    socket.on('error', () => upstream.destroy());
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return {
    url: 'http://127.0.0.1:' + server.address().port,
    close: async () => {
      for (const socket of tunnels) socket.destroy();
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    },
  };
}
