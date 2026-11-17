import { chromium } from 'playwright';
import { checkpoint } from './progress_report.mjs';
import { randomUUID } from 'node:crypto';
import { startOfflineProxy } from './offline_proxy.mjs';
import { waitForWorkloadData } from './readiness.mjs';
import { performance } from 'node:perf_hooks';

const [baseUrl, outputPath, executablePath, requestedRunId] = process.argv.slice(2);
const runId = requestedRunId || randomUUID();
if (!baseUrl || !outputPath || !executablePath) {
  throw new Error('usage: node browser_benchmark.mjs URL OUTPUT CHROME_EXECUTABLE');
}
const manifest = await fetch(baseUrl + '/__baseline__/manifest').then(r => r.json());
const offlineProxy = await startOfflineProxy(baseUrl);
const browser = await chromium.launch({ executablePath, headless: true, proxy: { server: offlineProxy.url } });
const results = [];
const errors = [];
const browserStarted = performance.now();
async function progress(stage) {
  await checkpoint(outputPath, { run_id: runId, status: 'running', results, errors },
    { ...stage, elapsed_seconds: (performance.now() - browserStarted) / 1000 });
}

async function stats(context, reset = false) {
  const response = reset
    ? await context.request.post(baseUrl + '/__baseline__/reset')
    : await context.request.get(baseUrl + '/__baseline__/stats');
  if (!response.ok()) throw new Error('stats endpoint failed: ' + response.status());
  return response.json();
}

async function settle(page, pending, timeoutMs = 15000) {
  const end = performance.now() + timeoutMs;
  while (pending.size) {
    if (performance.now() > end) throw new Error('API requests did not finish');
    await page.waitForTimeout(10);
  }
  await page.evaluate(() => new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

try {
  for (const [name, scenario] of Object.entries(manifest.page_cases)) {
    for (let sample = 0; sample < manifest.samples; sample++) {
      await progress({ case: name, sample, stage: 'context_begin' });
      const context = await browser.newContext({
        viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'UTC',
      });
      const login = await context.request.post(baseUrl + '/api/auth/login', { data: {
        username: 't00-page-' + name + '-' + String(sample).padStart(2, '0'),
        password: manifest.password,
      }});
      if (!login.ok()) throw new Error('login failed: ' + login.status());
      await progress({ case: name, sample, stage: 'login_completed' });
      const page = await context.newPage();
      const pending = new Set();
      let completed = [];
      let failures = [];
      let pageErrors = [];
      let started = 0;
      page.on('request', request => {
        if (new URL(request.url()).pathname.startsWith('/api/')) pending.add(request);
      });
      page.on('requestfinished', request => {
        if (pending.delete(request)) completed.push({
          path: new URL(request.url()).pathname, ms: performance.now() - started,
        });
      });
      page.on('requestfailed', request => {
        pending.delete(request);
        failures.push({ url: request.url(), error: request.failure()?.errorText });
      });
      page.on('response', response => {
        if (response.status() >= 400) failures.push({
          url: response.url(), status: response.status(),
        });
      });
      page.on('pageerror', error => pageErrors.push(error.message));
      for (const phase of ['first_visit', 'repeat_visit', 'refresh_button']) {
        await progress({ case: name, phase, sample, stage: 'phase_begin' });
        const before = await stats(context, true);
        completed = []; failures = []; pageErrors = [];
        started = performance.now();
        let firstDataMs = null;
        try {
          if (phase === 'refresh_button') {
            const primary = scenario.api[0];
            const [response] = await Promise.all([page.waitForResponse(response =>
              new URL(response.url()).pathname === primary && response.request().method() === 'GET',
              { timeout: 15000 }),
              page.getByRole('button', { name: /^刷\s*新$/ }).click({ timeout: 15000 }),
            ]);
            await response.finished();
          } else {
            await page.goto(baseUrl + scenario.path, { waitUntil: 'domcontentloaded' });
          }
          await waitForWorkloadData(page, scenario);
          await page.evaluate(() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve))));
          firstDataMs = performance.now() - started;
          await settle(page, pending);
          const completeMs = performance.now() - started;
          const after = await stats(context);
          const relevant = completed.filter(x => scenario.api.includes(x.path));
          if (!relevant.length && phase !== 'repeat_visit') {
            throw new Error('no workload API request completed');
          }
          const row = {
            case: name, phase, sample,
            first_data_ms: firstDataMs, complete_ms: completeMs,
            first_api_ms: relevant.length ? Math.min(...relevant.map(x => x.ms)) : null,
            complete_api_ms: completed.length ? Math.max(...completed.map(x => x.ms)) : null,
            api_requests: completed, aws_calls: after.calls,
            sql_queries: after.sql_queries, peak_concurrency: after.peak_concurrency,
            rss_bytes: after.rss_bytes, peak_rss_bytes: after.peak_rss_bytes,
            cpu_seconds: after.cpu_seconds - before.cpu_seconds,
            wall_seconds: after.clock_seconds - before.clock_seconds,
            violations: after.violations, failures, page_errors: pageErrors,
          };
          results.push(row);
          if (failures.length || pageErrors.length || after.violations.length) {
            errors.push({ case: name, phase, sample, failures, page_errors: pageErrors,
                          violations: after.violations });
          }
        } catch (error) {
          console.error(JSON.stringify({ case: name, phase, sample, error: error.message, url: page.url(), body: (await page.locator('body').innerText().catch(() => '')).slice(0, 1200) }));
          errors.push({ case: name, phase, sample, error: error.message,
                        failures, page_errors: pageErrors });
          results.push({ case: name, phase, sample, first_data_ms: firstDataMs,
                         error: error.message });
        }
        await progress({ case: name, phase, sample, stage: 'phase_end',
          phase_wall_seconds: (performance.now() - started) / 1000 });
      }
      await progress({ case: name, sample, stage: 'context_close_begin' });
      await context.close();
      await progress({ case: name, sample, stage: 'context_close_end' });
    }
    console.log('browser completed: ' + name);
  }
} finally {
  const report = {
    run_id: runId,
    status: !errors.length && results.length === Object.keys(manifest.page_cases).length * manifest.samples * 3
      ? 'completed' : 'failed',
    browser: browser.version(), executable: executablePath, viewport: [1440, 1000],
    locale: 'zh-CN', timezone: 'UTC', samples_per_case_phase: manifest.samples,
    http_cache: 'normal browser cache; request routing is not enabled',
    egress_guard: 'loopback proxy allows only the exact fixture origin, including CONNECT',
    fixture_sha256: manifest.fixture_sha256,
    first_visit_definition: 'fresh browser context and a distinct panel user for each case/sample',
    repeat_visit_definition: 'same context and user, full document navigation; no cache guarantee',
    first_data_definition: 'fixture business-data locator visible (not auth metadata), followed by two animation frames; see manifest ready_text',
    complete_definition: 'all observed API requests finished, followed by two animation frames',
    results, errors,
  };
  await checkpoint(outputPath, report, { stage: 'browser_end',
    elapsed_seconds: (performance.now() - browserStarted) / 1000 });
  await browser.close();
  await offlineProxy.close();
}
if (errors.length) {
  console.error(JSON.stringify(errors.slice(0, 5), null, 2));
  process.exitCode = 1;
}
