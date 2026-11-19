import { test } from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import net from 'node:net'
import { once } from 'node:events'
import { startOfflineProxy } from './offline_proxy.mjs'

test('connection resets leave allowlist proxy usable and external targets blocked', async () => {
  const server=http.createServer((_req,res) => res.end('fixture'))
  server.listen(0,'127.0.0.1');await once(server,'listening')
  const origin=`http://127.0.0.1:${server.address().port}`
  const proxy=await startOfflineProxy(origin)
  try {
    const address=new URL(proxy.url)
    for (let i=0;i<50;i++) {
      const socket=net.connect(Number(address.port),address.hostname)
      socket.on('error',()=>{})
      await once(socket,'connect')
      socket.write('CONNECT example.invalid:443 HTTP/1.1\r\nHost: example.invalid:443\r\n\r\n')
      socket.resetAndDestroy()
    }
    const get=url=>new Promise((resolve,reject)=>{
      const request=http.request(proxy.url,{path:url,headers:{host:new URL(url).host}},res=>{
        let body='';res.on('data',chunk=>body+=chunk);res.on('end',()=>resolve({status:res.statusCode,body}))
      });request.on('error',reject);request.end()
    })
    assert.deepEqual(await get(origin+'/'),{status:200,body:'fixture'})
    assert.equal((await get('http://example.invalid/')).status,403)
  } finally {
    await proxy.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve))
  }
})
