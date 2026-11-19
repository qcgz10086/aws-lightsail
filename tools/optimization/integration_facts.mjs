export function responseFacts(name, data, generation, refreshedAfter = 0, credentials = []) {
  const fresh = cache => cache?.status === 'fresh' && !cache.stale && !cache.error && !cache.refreshing
    && Date.parse(cache.last_success_at) >= refreshedAfter
  if (name === 'billing') {
    if (data.accounts) {
      const costs = data.accounts.filter(a => a.costs?.status === 'ok' && fresh(a.costs.cache) &&
        a.costs.currencies?.some(c => c.currency === 'USD' && Number(c.total_cost) === 30 + generation))
      const allOk = data.accounts.length === 2 && ['111111111111','222222222222'].every(id => data.accounts.some(a => a.account_id === id))
        && data.accounts.every(a => ['costs', 'credits', 'forecast'].every(k => a[k]?.status === 'ok' && fresh(a[k].cache)))
      const total = data.currencies?.find(c => c.currency === 'USD')?.total_cost
      return { first: costs.length > 0, complete: allOk && costs.length === 2 && !data.refreshing
        && data.currencies.length === 1 && Number(total) === 2 * (30 + generation),
        candidate: true, amount: data.currencies?.find(c => c.currency === 'USD')?.total_cost, status: data.status,
        accounts: data.accounts.map(a => ({ id: a.account_id, ...Object.fromEntries(
          ['costs','forecast','credits'].map(k => [k, {status: a[k]?.status, cache: a[k]?.cache}])) })) }
    }
    const valid = data.status === 'ok' && Number(data.total_cost) === 30 + generation && data.aws_credits_status === 'ok'
    return { first: valid, complete: valid, candidate: false, amount: data.total_cost, status: data.status }
  }
  const candidate = !Array.isArray(data)
  const rows = candidate ? data.instances || [] : data
  const current = rows.filter(i => i.blueprint_name === `Fixture-generation-${generation}`)
  const expected = new Map([
    ['111111111111:us-east-1', candidate ? 6 : 12], ['111111111111:ap-northeast-1', candidate ? 6 : 12],
    ['222222222222:us-east-1', 6], ['222222222222:ap-northeast-1', 6]])
  const counts = new Map()
  for (const row of current) {
    const key = `${row.account_id || row.name.split('-')[1]}:${row.region}`
    counts.set(key, (counts.get(key) || 0) + 1)
  }
  const selected = candidate ? credentials.filter(c => c.selected) : credentials
  const expectedKeys = new Set(selected.flatMap(c => ['us-east-1','ap-northeast-1'].flatMap(region =>
    Array.from({length:6}, (_, n) => `${c.id}/${region}/t00-${c.account_id}-${region}-${String(n).padStart(2,'0')}`))))
  const keys = new Set(current.map(i => `${i.credential_id}/${i.region}/${i.name}`))
  const expectedScopes = new Set(selected.flatMap(c => ['us-east-1','ap-northeast-1'].map(region => `${c.id}/${c.account_id}/${region}`)))
  const scopes = new Set((data.scopes || []).map(s => `${s.credential_id}/${s.account_id}/${s.region}`))
  const complete = current.length === rows.length && counts.size === expected.size
    && [...expected].every(([key, count]) => counts.get(key) === count)
    && keys.size === rows.length && keys.size === expectedKeys.size && [...expectedKeys].every(k => keys.has(k))
    && (!candidate || (!data.refreshing && data.scopes?.length === 4 && scopes.size === expectedScopes.size
      && [...expectedScopes].every(s => scopes.has(s))
      && data.scopes.every(s => fresh(s))))
  return { first: current.length > 0, complete, candidate, count: rows.length,
    running: rows.filter(i => i.state === 'running').length, generation, scopes: data.scopes,
    rows: rows.map(i => ({key: `${i.credential_id}/${i.region}/${i.name}`, name:i.name,
      ip: i.public_ip, state: i.state, current: i.blueprint_name === `Fixture-generation-${generation}`})) }
}
