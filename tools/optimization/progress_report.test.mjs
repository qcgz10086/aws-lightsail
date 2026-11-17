import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { checkpoint } from './progress_report.mjs';

test('checkpoints retain completed samples when the next stage fails', async () => {
  const directory = await mkdtemp(join(tmpdir(), 't00-checkpoint-'));
  try {
    const path = join(directory, 'browser.json');
    const report = { run_id: 'test-run', status: 'running', results: [{ sample: 0 }], errors: [] };
    await checkpoint(path, report, { stage: 'begin', sample: 1 });
    const saved = JSON.parse(await readFile(path, 'utf8'));
    assert.equal(saved.status, 'running');
    assert.equal(saved.results.length, 1);
    assert.equal(saved.current_stage.sample, 1);
    report.status = 'failed';
    report.errors.push({ error: 'timeout' });
    await checkpoint(path, report, { stage: 'failed' });
    const failed = JSON.parse(await readFile(path, 'utf8'));
    assert.equal(failed.results.length, 1);
    assert.equal(failed.errors[0].error, 'timeout');
    const events = (await readFile(path + '.progress.jsonl', 'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(events.length, 2);
    assert.ok(events.every(event => event.run_id === 'test-run' && event.at_utc));
  } finally {
    await rm(directory, { recursive: true });
  }
});
