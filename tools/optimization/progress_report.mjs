import { appendFile, rename, writeFile } from 'node:fs/promises';

export async function checkpoint(path, report, stage) {
  const event = { run_id: report.run_id, at_utc: new Date().toISOString(), ...stage,
    completed_samples: report.results.length, errors: report.errors.length };
  await writeFile(path + '.tmp', JSON.stringify({ ...report, current_stage: event }, null, 2) + '\n');
  await rename(path + '.tmp', path);
  await appendFile(path + '.progress.jsonl', JSON.stringify(event) + '\n');
  console.log(JSON.stringify(event));
}
