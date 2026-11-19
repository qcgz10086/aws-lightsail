import { test } from 'node:test'
import assert from 'node:assert/strict'
import { responseFacts } from './integration_facts.mjs'

const cache = {status:'fresh', stale:false, refreshing:false, error:null, last_success_at:'2026-11-19T00:00:02Z'}
test('billing separates old default-account result from aggregate and rejects stale sections', () => {
  assert.equal(responseFacts('billing', {status:'ok', total_cost:31, aws_credits_status:'ok'}, 1).complete, true)
  assert.equal(responseFacts('billing', {status:'ok', total_cost:62, aws_credits_status:'ok'}, 1).complete, false)
  const data = {accounts:['111111111111','222222222222'].map(account_id => ({account_id,
    costs:{status:'ok', cache:{...cache}, currencies:[{currency:'USD', total_cost:'31'}]},
    credits:{status:'ok',cache:{...cache}}, forecast:{status:'ok',cache:{...cache}}})), currencies:[{currency:'USD',total_cost:'62'}]}
  const start = Date.parse('2026-11-19T00:00:01Z')
  assert.equal(responseFacts('billing', data, 1, start).complete, true)
  data.currencies[0].total_cost = '999'
  assert.equal(responseFacts('billing', data, 1, start).complete, false)
  data.currencies[0].total_cost = '62'
  data.accounts[0].forecast.cache.last_success_at = '2026-11-19T00:00:00Z'
  assert.equal(responseFacts('billing', data, 1, start).complete, false)
  for (const property of ['stale','refreshing','error']) {
    data.accounts[0].forecast.cache = {...cache, [property]:true}
    assert.equal(responseFacts('billing', data, 1).complete, false)
  }
})
test('instance completion enforces legacy duplicates, candidate deduplication and every scope', () => {
  const credentials = ['111111111111','111111111111','222222222222'].map((account_id, i) => ({id:i+1,account_id,selected:i!==1}))
  const rows = duplicate => credentials.filter(c => duplicate || c.selected)
    .flatMap(c => ['us-east-1','ap-northeast-1'].flatMap(region => Array.from({length:6}, (_, n) => ({
      account_id:c.account_id, credential_id:c.id, name:`t00-${c.account_id}-${region}-${String(n).padStart(2,'0')}`, region,
      blueprint_name:'Fixture-generation-1', state:'running', public_ip:`203.0.1.${n+1}`}))))
  const facts = data => responseFacts('instances',data,1,0,credentials)
  assert.equal(facts(rows(true)).complete,true)
  assert.equal(facts(rows(false)).complete,false)
  const scopes=credentials.filter(c => c.selected).flatMap(c => ['us-east-1','ap-northeast-1'].map(region => ({region,credential_id:c.id,account_id:c.account_id,...cache})))
  const data={instances:rows(false),scopes,refreshing:false}
  assert.equal(facts(data).complete,true)
  assert.equal(facts({...data,scopes:Array(4).fill(scopes[0])}).complete,false)
  assert.equal(facts({...data,instances:rows(false).map((r,i) => i===1 ? data.instances[0] : r)}).complete,false)
  data.instances.pop()
  assert.equal(responseFacts('instances',data,1).complete,false)
  assert.equal(responseFacts('instances',data,1).first,true)
  assert.equal(responseFacts('instances',data,2).first,false)
})
