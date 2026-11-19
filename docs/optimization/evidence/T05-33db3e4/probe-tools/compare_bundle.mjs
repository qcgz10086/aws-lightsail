import fs from 'node:fs/promises'
import path from 'node:path'
import crypto from 'node:crypto'
import zlib from 'node:zlib'
import { createRequire } from 'node:module'

const [distArg, previousArg, dependencies, head, output] = process.argv.slice(2)
const dist = path.resolve(distArg)
const ts = createRequire(import.meta.url)(path.join(path.resolve(dependencies), 'typescript'))
const previous = JSON.parse(await fs.readFile(previousArg, 'utf8'))
const html = await fs.readFile(path.join(dist, 'index.html'), 'utf8')
const entry = html.match(/<script[^>]+src="\/?([^"]+\.js)"/)[1]
const names = (await fs.readdir(path.join(dist, 'assets'))).filter(n => n.endsWith('.js')).map(n => `assets/${n}`)
async function closure(initial) {
  const visited = new Set()
  async function visit(name) {
    if (visited.has(name)) return
    visited.add(name)
    const text = await fs.readFile(path.join(dist, name), 'utf8')
    const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
    for (const stmt of source.statements) if (ts.isImportDeclaration(stmt) || ts.isExportDeclaration(stmt)) {
      const target = stmt.moduleSpecifier?.text
      if (target?.startsWith('.')) await visit(path.posix.normalize(path.posix.join(path.posix.dirname(name), target)))
    }
  }
  for (const initialName of initial) await visit(initialName)
  const files = []
  for (const name of [...visited].sort()) {
    const data = await fs.readFile(path.join(dist, name))
    files.push({ name, bytes: data.length, gzip_bytes: zlib.gzipSync(data).length,
      sha256: crypto.createHash('sha256').update(data).digest('hex') })
  }
  return { bytes: files.reduce((s, x) => s+x.bytes, 0), gzip_bytes: files.reduce((s,x) => s+x.gzip_bytes,0), files }
}
const candidate = { entry, static_entry_closure: await closure([entry]),
  dashboard_static_closure: await closure([entry, names.find(n => /\/Dashboard-/.test(n))]), all_js: await closure(names) }
const result = { base_ref: previous.base_ref, candidate_head: head, method: previous.method, base: previous.base, candidate }
await fs.writeFile(output, JSON.stringify(result, null, 2))
console.log(JSON.stringify(Object.fromEntries(Object.entries(candidate).map(([k,v]) => [k, typeof v === 'string' ? v : { bytes:v.bytes, gzip_bytes:v.gzip_bytes }])), null, 2))
