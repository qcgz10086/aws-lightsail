import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { performance } from 'node:perf_hooks';
import { waitForWorkloadData } from './readiness.mjs';

test('credential count cannot finish the dashboard business-data timer', async () => {
  const browser = await chromium.launch({
    executablePath: process.env.T00_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.setContent(
      '<div class="ant-statistic">绑定凭证数<span class="ant-statistic-content-value">3</span></div>' +
      '<div class="ant-statistic">实例总数<span id="instances" class="ant-statistic-content-value">0</span></div>');
    let settled = false;
    const waiting = waitForWorkloadData(page, {
      ready: '.ant-statistic-content-value', ready_text: '实例总数',
    }).then(() => { settled = true; });
    waiting.catch(() => {});
    await page.waitForTimeout(350);
    assert.equal(settled, false, 'authentication metadata is not workload data');
    const changedAt = performance.now();
    await page.locator('#instances').evaluate(element => element.textContent = '36');
    await waiting;
    assert.ok(performance.now() - changedAt < 200, 'readiness detection must not add locator retry backoff');
    assert.equal(await page.locator('#instances').textContent(), '36');
  } finally {
    await browser.close();
  }
});
