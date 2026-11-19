import { chromium } from 'playwright'
import fs from 'node:fs/promises'
import { performance } from 'node:perf_hooks'
import { startOfflineProxy } from './offline_proxy.mjs'
import { randomUUID } from 'node:crypto'
import { responseFacts } from './integration_facts.mjs'

const [origin, output, chrome, requestedRunId] = process.argv.slice(2)
const runId = requestedRunId || randomUUID()
const manifest = await fetch(origin + '/__baseline__/manifest').then(r => r.json())
const identities = await fetch(origin + '/__baseline__/identities').then(r => r.json())
const proxy = await startOfflineProxy(origin)
const browser = await chromium.launch({ executablePath: chrome, headless: true, proxy: { server: proxy.url } })
const results = [], errors = []
const controls = []
async function control(context, path) {
  const response = await context.request.post(origin + path)
  if (!response.ok()) throw new Error(`Control ${path}: HTTP ${response.status()}`)
  const data = await response.json()
  controls.push({ path, status: response.status(), at: performance.now(), data })
  return data
}
const report = async status => fs.writeFile(output, JSON.stringify({ run_id: runId, expected_rows: manifest.samples * 12,
  status, fixture: manifest, identities, controls, browser: browser.version(),
  definitions: { first: 'Current generation business value visible after workload API response; two animation frames',
    complete: 'Current generation all scopes terminal and business DOM visible; observed API pending empty',
    generations: 'Every cold user starts generation 1; repeat uses same; refresh advances to 2; unavailable is a failure',
    phases: 'Fresh context/user, same-context full reload, explicit refresh, SPA link to another page then back' },
  results, errors }, null, 2))

async function visible(page, name, facts) {
  return page.evaluate(({ name, f }) => {
    const text = document.body.innerText
    if (name === 'instances') {
      const rows = [...document.querySelectorAll('.ant-table-tbody .ant-table-row')]
      const matches = row => f.rows.find(r => r.key === row.getAttribute('data-row-key') &&
        row.innerText.includes(r.ip) && row.innerText.includes(r.state))
      const first = rows.some(row => matches(row)?.current)
      if (!f.complete) return first
      const pages = [...document.querySelectorAll('.ant-pagination-item')].map(p => Number(p.title)).filter(Number.isFinite)
      return first && rows.length === Math.min(10, f.count) && rows.every(matches)
        && Math.max(1, ...pages) === Math.ceil(f.count / 10)
    }
    const stats = [...document.querySelectorAll('.ant-statistic')]
    const value = title => stats.find(x => x.innerText.includes(title))?.querySelector('.ant-statistic-content-value')?.textContent?.replaceAll(',', '')
    if (name === 'dashboard') return Number(value('实例总数')) === f.count && Number(value('运行中')) === f.running
    if (f.candidate) return [...document.querySelectorAll('.ant-card')].some(c => c.innerText.includes('费用汇总') && c.innerText.includes(`USD ${f.amount}`))
    return Number(value('本月总费用')?.replace(/[^\d.]/g, '')) === Number(f.amount)
  }, { name, f: facts })
}
try {
  for (const [name, scenario] of Object.entries(manifest.page_cases)) {
    for (let sample = 0; sample < manifest.samples; sample++) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'UTC' })
      const username = `t00-page-${name}-${String(sample).padStart(2, '0')}`
      await control(context, '/__baseline__/generation/1')
      const login = await context.request.post(origin + '/api/auth/login', { data: {
        username, password: manifest.password } })
      if (!login.ok()) throw new Error('login failed')
      const page = await context.newPage()
      const pending = new Set()
      let observations = [], failures = [], pageErrors = [], finished = [], started = 0, generation = 1, lastFacts = null, refreshedAfter = 0
      page.on('request', req => { if (new URL(req.url()).pathname.startsWith('/api/')) pending.add(req) })
      page.on('requestfinished', req => { if (pending.delete(req)) finished.push({ path: new URL(req.url()).pathname,
        method: req.method(), ms: performance.now() - started }) })
      page.on('requestfailed', req => { pending.delete(req); if (req.failure()?.errorText !== 'net::ERR_ABORTED') failures.push(req.failure()) })
      page.on('pageerror', err => pageErrors.push(String(err)))
      page.on('response', async res => {
        const p = new URL(res.url()).pathname
        if (!p.startsWith('/api/')) return
        if (res.status() >= 400) failures.push({ path: p, status: res.status() })
        const relevant = name === 'billing' ? ['/api/billing/summary', '/api/v2/billing/summary'] : ['/api/instances', '/api/v2/instances']
        if (!relevant.includes(p) || res.request().method() !== 'GET') return
        try {
          const data = await res.json()
          const facts = responseFacts(name, data, generation, refreshedAfter, identities[username])
          lastFacts = facts
          observations.push({ path: p, ms: performance.now() - started, ...facts })
        } catch (error) { failures.push({ path: p, error: String(error) }) }
      })
      for (const phase of ['first_visit', 'repeat_visit', 'refresh_button', 'spa_return']) {
        if (phase === 'spa_return') {
          await page.getByRole('link', { name: name === 'instances' ? '总览' : '实例', exact: true }).click()
          await page.waitForTimeout(100)
          const limit = performance.now() + 30000
          while (pending.size && performance.now() < limit) await page.waitForTimeout(10)
          if (pending.size) throw new Error('SPA departure did not settle')
        }
        generation = phase === 'first_visit' || phase === 'repeat_visit' ? 1 : 2
        const clock = await control(context, `/__baseline__/generation/${generation}`)
        if (phase === 'first_visit' || phase === 'refresh_button') refreshedAfter = clock.epoch_ms
        const before = await control(context, '/__baseline__/reset')
        observations = []; failures = []; pageErrors = []; finished = []
        if (phase !== 'spa_return') lastFacts = null
        started = performance.now()
        let first = null, complete = null
        try {
          if (phase === 'refresh_button') await page.getByRole('button', { name: /^刷\s*新$/ }).click()
          else if (phase === 'spa_return') await page.getByRole('link', { name: { dashboard: '总览', instances: '实例', billing: '账单' }[name], exact: true }).click()
          else await page.goto(origin + scenario.path, { waitUntil: 'domcontentloaded' })
          while (performance.now() - started < 30000) {
            if (lastFacts?.first && await visible(page, name, lastFacts)) {
              await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
              if (first === null) first = performance.now() - started
              if (lastFacts.complete && pending.size === 0) { complete = performance.now() - started; break }
            }
            await page.waitForTimeout(10)
          }
          if (complete === null) throw new Error('Current generation business data did not complete: ' + JSON.stringify(lastFacts))
          const after = await context.request.get(origin + '/__baseline__/stats').then(r => r.json())
          results.push({ case: name, phase, sample, generation, first_data_ms: first, complete_ms: complete,
            observations, api_requests: finished, aws_calls: after.calls, peak_concurrency: after.peak_concurrency,
            sql_queries: after.sql_queries, violations: after.violations, failures, page_errors: pageErrors,
            wall_seconds: after.clock_seconds - before.clock_seconds, cpu_seconds: after.cpu_seconds - before.cpu_seconds })
          if (after.violations.length || failures.length || pageErrors.length) errors.push({ case: name, phase, sample, violations: after.violations, failures, pageErrors })
        } catch (error) {
          const failure = { case: name, phase, sample, error: String(error), observations, failures, pageErrors }
          results.push(failure); errors.push(failure)
          await page.screenshot({ path: output + `.${name}-${sample}-${phase}.png`, fullPage: true })
        }
        await report('running')
      }
      await context.close()
      console.log(name, sample, results.slice(-4).map(r => r.error || Math.round(r.first_data_ms)))
    }
  }
} finally {
  await report(errors.length ? 'failed' : 'completed')
  await browser.close(); await proxy.close()
}
if (errors.length) process.exitCode = 1
