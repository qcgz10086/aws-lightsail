import crypto from 'node:crypto'
import fs from 'node:fs/promises'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const frontend = path.join(root, 'frontend')
const evidenceDir = path.join(root, 'docs/optimization/evidence/T05-33db3e4')
const toolsDir = path.join(evidenceDir, 'probe-tools')
const PRODUCT = '33db3e4f6b04653b93d60e9e88f53d11d785ec94'
const PRODUCT_TREE = '9d4dd144c4fc3c6d426fd03c835b63099f5b47fd'
const PROJECT_BASE = '234d1a91e8e24934d2730b4a5a88c8205ac1f649'
const PRODUCT_PARENT = 'bcb2da62c8c2c1fd171c1c4673165c1a2d27969d'
const [outputArg, playwrightArg] = process.argv.slice(2)
if (!outputArg) throw new Error('Usage: node tools/optimization/verify_t05_frontend.mjs OUTPUT_DIR [PLAYWRIGHT_PACKAGE]')
const output = path.resolve(outputArg)
if (!output.startsWith(`${root}${path.sep}`) || output === root) throw new Error('OUTPUT_DIR must be inside repository')
await fs.mkdir(path.dirname(output), { recursive: true })
await fs.mkdir(output)

function git(args, cwd = root) {
  const res = spawnSync('git', args, { cwd, encoding: 'utf8', windowsHide: true })
  if (res.status !== 0) throw new Error(res.stderr || `git ${args.join(' ')} failed`)
  return res.stdout.trim()
}
function gitBytes(args) {
  const res = spawnSync('git', args, { cwd: root, encoding: null, windowsHide: true, maxBuffer: 64 * 1024 * 1024 })
  if (res.status !== 0) throw new Error(res.stderr?.toString() || `git ${args.join(' ')} failed`)
  return res.stdout
}
function run(name, executable, args, cwd, log) {
  const start = Date.now()
  const res = spawnSync(executable, args, { cwd, encoding: 'utf8', windowsHide: true })
  const record = { name, executable, args, cwd, exitCode: res.status ?? 1,
    durationMs: Date.now() - start, log }
  return fs.mkdir(path.dirname(path.join(output, log)), { recursive: true })
    .then(() => fs.writeFile(path.join(output, log), `${res.stdout || ''}${res.stderr || ''}`))
    .then(() => {
      if (record.exitCode !== 0) throw new Error(`${name} failed; see ${path.join(output, log)}`)
      return record
    })
}
const hash = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex')
const hashFile = async (file) => hash(await fs.readFile(file))
const readJson = async (file) => JSON.parse(await fs.readFile(file, 'utf8'))
const assertPassedRows = (label, rows, expected) => {
  if (!Array.isArray(rows) || rows.length !== expected || rows.some((row) => row.passed !== true)) {
    throw new Error(`${label}: expected ${expected} passing rows`)
  }
}
const runs = []

const deliveryHead = git(['rev-parse', 'HEAD'])
const deliveryTree = git(['rev-parse', 'HEAD^{tree}'])
if (git(['rev-parse', `${PRODUCT}^{tree}`]) !== PRODUCT_TREE) throw new Error('Fixed product tree mismatch')
if (git(['rev-parse', `${PRODUCT}^`]) !== PRODUCT_PARENT) throw new Error('Fixed product parent mismatch')
if (spawnSync('git', ['merge-base', '--is-ancestor', PROJECT_BASE, PRODUCT], { cwd: root, windowsHide: true }).status !== 0) {
  throw new Error('Project BASE is not ancestor of product HEAD')
}
if (git(['diff', '--name-only', PROJECT_BASE, PRODUCT, '--', 'backend', 'tools']) !== '') {
  throw new Error('Product changes backend/tools; T05 frontend evidence is inapplicable')
}
if (git(['diff', '--name-only', deliveryHead, PRODUCT, '--', 'backend', 'frontend']) !== '') {
  throw new Error('Delivery checkout backend/frontend differs from fixed product HEAD')
}
const frontendTree = gitBytes(['ls-tree', '-r', '-z', PRODUCT, '--', 'frontend']).toString('utf8')
const trackedFrontend = frontendTree.split('\0').filter(Boolean).map((record) => {
  const [modeTypeHash, rel] = record.split('\t')
  const [mode, type, blob] = modeTypeHash.split(' ')
  if (type !== 'blob') throw new Error(`Unexpected product entry: ${rel}`)
  return { mode, type, blob, path: rel }
})
for (const entry of trackedFrontend) {
  const expected = gitBytes(['cat-file', 'blob', entry.blob])
  const actual = await fs.readFile(path.join(root, entry.path))
  const normalized = Buffer.from(actual.toString('utf8').replace(/\r\n/g, '\n'), 'utf8')
  if (!actual.equals(expected) && !normalized.equals(expected)) {
    throw new Error(`Worktree product content differs from fixed product blob: ${entry.path}`)
  }
}

const identity = { projectBase: PROJECT_BASE, productHead: PRODUCT, productTree: PRODUCT_TREE,
  productParent: PRODUCT_PARENT, deliveryHead, deliveryTree, deliveryProductDirectoriesMatch: true,
  verifiedFrontendBlobs: trackedFrontend.length, nodeVersion:
    spawnSync(process.execPath, ['--version'], { encoding: 'utf8' }).stdout.trim() }
const archivePath = path.join(output, 'product-source.tar')
const archive = spawnSync('git', ['archive', '-o', archivePath, PRODUCT], { cwd: root, windowsHide: true })
if (archive.status !== 0) throw new Error('git archive failed')
identity.sourceArchiveSha256 = await hashFile(archivePath)
await fs.writeFile(path.join(output, 'identity.json'), JSON.stringify(identity, null, 2) + '\n')

runs.push(await run('cache-unit-tests', process.execPath, ['--test', 'tests/queryCache.test.mjs'], frontend, 'cache-tests.log'))
const testLog = await fs.readFile(path.join(output, 'cache-tests.log'), 'utf8')
if (!testLog.includes('# tests 9') || !testLog.includes('# pass 9') || !testLog.includes('# fail 0')) {
  throw new Error('Expected 9 passing cache unit tests')
}
runs.push(await run('typescript', process.execPath,
  ['node_modules/typescript/bin/tsc', '--noEmit'], frontend, 'typescript.log'))
const dist = path.join(output, 'frontend-build')
runs.push(await run('vite-build', process.execPath,
  ['node_modules/vite/bin/vite.js', 'build', '--outDir', dist], frontend, 'build.log'))

const queryCachePath = path.join(frontend, 'src/api/queryCache.mjs')
const queryCache = await run('cache-deterministic-probes', process.execPath,
  [path.join(toolsDir, 'probe_query_cache.mjs'), queryCachePath,
    path.join(output, 'query-cache-probe.json')], root, 'query-cache-probe.log')
runs.push(queryCache)
const queryResult = await readJson(path.join(output, 'query-cache-probe.json'))
assertPassedRows('query-cache probe', queryResult.records, 4)

const adapterDir = path.join(output, 'adapter')
runs.push(await run('axios-adapter-probes', process.execPath,
  [path.join(toolsDir, 'probe_api_cache.mjs'), frontend, path.join(frontend, 'node_modules'), adapterDir],
  root, 'adapter.log'))
const adapterResult = await readJson(path.join(adapterDir, 'result.json'))
assertPassedRows('adapter probe', adapterResult.records, 4)
if (adapterResult.passed !== 4 || adapterResult.total !== 4) throw new Error('Adapter summary mismatch')

if (playwrightArg) {
  const playwright = path.resolve(playwrightArg)
  const catalogDir = path.join(output, 'browser-catalog')
  runs.push(await run('catalog-browser', process.execPath,
    [path.join(toolsDir, 'browser_catalog_probe.mjs'), dist, catalogDir, playwright], root, 'browser-catalog.log'))
  const catalog = await readJson(path.join(catalogDir, 'browser-catalog.json'))
  assertPassedRows('catalog browser', catalog.results, 12)
  if (catalog.errors.length !== 0) throw new Error('Catalog browser emitted page errors')

  const browserDir = path.join(output, 'browser-regression')
  runs.push(await run('frontend-browser', process.execPath,
    [path.join(toolsDir, 'browser_probe.mjs'), dist, browserDir, playwright], root, 'browser-regression.log'))
  const browser = await readJson(path.join(browserDir, 'browser.json'))
  const browserRows = browser.results.filter((row) => row.scenario !== 'dashboard_to_instances_shared_query')
  if (browser.results.length !== 11 || browserRows.some((row) => row.passed !== true)
    || browser.results.some((row) => row.scenario === 'probe_execution') || browser.pageErrors.length !== 0) {
    throw new Error('Frontend browser assertions failed')
  }
  const shared = browser.results.find((row) => row.scenario === 'dashboard_to_instances_shared_query')
  if (shared?.additional_http_queries !== 0) throw new Error('Dashboard-to-instances added an HTTP query')
}

const baseManifest = path.join(evidenceDir, 'base-bundle-manifest.json')
runs.push(await run('bundle-comparison', process.execPath,
  [path.join(toolsDir, 'compare_bundle.mjs'), dist, baseManifest, path.join(frontend, 'node_modules'),
    PRODUCT, path.join(output, 'bundle-comparison.json')], root, 'bundle-comparison.log'))
const bundle = await readJson(path.join(output, 'bundle-comparison.json'))
if (bundle.candidate_head !== PRODUCT || !bundle.candidate.static_entry_closure?.bytes
  || !bundle.candidate.all_js?.bytes) throw new Error('Bundle comparison identity/assertion failed')

async function walk(dir, relative = '') {
  const result = []
  const entries = await fs.readdir(dir, { withFileTypes: true })
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const rel = path.posix.join(relative, entry.name)
    const absolute = path.join(dir, entry.name)
    if (entry.isDirectory()) result.push(...await walk(absolute, rel))
    else result.push({ path: rel, bytes: (await fs.stat(absolute)).size, sha256: await hashFile(absolute) })
  }
  return result
}
const buildAssets = await walk(dist)
const sourceFiles = []
for (const { path, blob } of trackedFrontend) {
  const bytes = gitBytes(['cat-file', 'blob', blob])
  sourceFiles.push({ path, gitBlob: blob, sha256: hash(bytes) })
}
const probeTools = []
for (const file of await fs.readdir(toolsDir)) {
  if (!file.endsWith('.mjs')) continue
  const rel = path.posix.join('docs/optimization/evidence/T05-33db3e4/probe-tools', file)
  probeTools.push({ path: rel, sha256: await hashFile(path.join(root, rel)) })
}
probeTools.push({ path: 'tools/optimization/verify_t05_frontend.mjs',
  sha256: await hashFile(path.join(root, 'tools/optimization/verify_t05_frontend.mjs')) })
for (const rel of ['tools/optimization/verify_t05_frontend.mjs',
  'docs/optimization/evidence/T05-33db3e4/base-bundle-manifest.json']) {
  probeTools.push({ path: rel, sha256: await hashFile(path.join(root, rel)) })
}
const evidenceIndex = { ...identity, testRuns: runs, productFiles: sourceFiles,
  buildAssets, probeTools, limits: [
    'Browser scenarios use localhost synthetic API data and intercept resource creation; no real AWS transport runs.',
    'Backend/T04 suites were not rerun: BASE..product changes no backend/tools; final integration must rerun them.',
    'Vite retains a >500 kB shared chunk warning; no production timing, image, CPU, or memory claim is made.',
  ] }
await fs.writeFile(path.join(output, 'evidence-index.json'), JSON.stringify(evidenceIndex, null, 2) + '\n')
console.log(JSON.stringify({ ...identity, runs, productFiles: sourceFiles.length, buildAssets: buildAssets.length }, null, 2))
