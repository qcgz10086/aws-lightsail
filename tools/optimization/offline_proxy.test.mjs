import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { startOfflineProxy } from './offline_proxy.mjs';

function get(proxyUrl, target) {
  const proxy = new URL(proxyUrl);
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: proxy.hostname, port: proxy.port, path: target, method: 'GET',
    }, res => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => resolve({ status: res.statusCode, body }));
    });
    req.on('error', reject);
    req.end();
  });
}

test('offline proxy forwards only the exact fixture origin', async () => {
  const upstream = http.createServer((req, res) => res.end('fixture:' + req.url));
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  const target = 'http://127.0.0.1:' + upstream.address().port;
  const proxy = await startOfflineProxy(target);
  try {
    assert.deepEqual(await get(proxy.url, target + '/api/health'), {
      status: 200, body: 'fixture:/api/health',
    });
    assert.equal((await get(proxy.url, 'http://192.0.2.10/')).status, 403);
    assert.equal((await get(proxy.url, 'http://127.0.0.1:9/')).status, 403);
    const connectReply = await new Promise((resolve, reject) => {
      const socket = net.connect(new URL(proxy.url).port, '127.0.0.1', () =>
        socket.write('CONNECT aws.example.invalid:443 HTTP/1.1\r\nHost: aws.example.invalid\r\n\r\n'));
      socket.on('data', chunk => { resolve(chunk.toString()); socket.destroy(); });
      socket.on('error', reject);
    });
    assert.match(connectReply, /^HTTP\/1.1 403/);
    const localTunnel = await new Promise((resolve, reject) => {
      let received = '';
      let sent = false;
      const socket = net.connect(new URL(proxy.url).port, '127.0.0.1', () =>
        socket.write('CONNECT ' + new URL(target).host + ' HTTP/1.1\r\n\r\n'));
      socket.on('data', chunk => {
        received += chunk.toString();
        if (!sent && received.includes('\r\n\r\n')) {
          if (!received.startsWith('HTTP/1.1 200')) {
            resolve(received); socket.destroy(); return;
          }
          sent = true;
          socket.write('GET /tunnel HTTP/1.1\r\nHost: ' + new URL(target).host + '\r\nConnection: close\r\n\r\n');
        }
        if (received.includes('fixture:/tunnel')) { resolve(received); socket.destroy(); }
      });
      socket.on('error', reject);
    });
    assert.match(localTunnel, /fixture:\/tunnel/);
  } finally {
    await proxy.close();
    await new Promise(resolve => upstream.close(resolve));
  }
});
