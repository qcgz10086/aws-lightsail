import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { build, createServer, loadConfigFromFile } from 'vite'

const configPath = fileURLToPath(new URL('../vite.config.ts', import.meta.url))
const loaded = await loadConfigFromFile({ command: 'build', mode: 'production' }, configPath)
const configuredPlugins = loaded.config.plugins.flat(Infinity)

async function withFixture(run) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'lightsail-inline-styles-'))
  try {
    await Promise.all([
      writeFile(path.join(root, 'index.html'), '<!doctype html><html><head></head><body><div id="app"></div><script type="module" src="/main.js"></script></body></html>'),
      writeFile(path.join(root, 'main.js'), 'import "./entry.css"; import { value } from "./shared.js"; window.initialValue = value; window.loadLazy = () => import("./lazy.js")'),
      writeFile(path.join(root, 'entry.css'), '.initial-marker { color: rgb(12, 34, 56); }'),
      writeFile(path.join(root, 'lazy.js'), 'import "./lazy.css"; export const lazyValue = 7'),
      writeFile(path.join(root, 'lazy.css'), '.lazy-marker { color: rgb(65, 43, 21); }'),
      writeFile(path.join(root, 'shared.js'), 'export const value = "shared"'),
    ])
    await run(root)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
}

async function buildFixture(root, { beforeInline, inline = true, base = '/' } = {}) {
  const result = await build({
    configFile: false,
    root,
    base,
    logLevel: 'silent',
    plugins: [
      { name: 'fixture-html-transform', transformIndexHtml: { order: 'post', handler: beforeInline || ((html) => html) } },
      ...configuredPlugins.filter((plugin) => inline || plugin.name !== 'inline-initial-styles'),
    ],
    build: {
      write: false,
      minify: false,
      cssMinify: false,
      rollupOptions: { output: { manualChunks: { shared: [path.join(root, 'shared.js')] } } },
    },
  })
  const output = result.output
  const html = output.find((item) => item.type === 'asset' && item.fileName === 'index.html').source
  return { output, html }
}

function replaceInitialCss(css) {
  return (html, { bundle }) => {
    const initial = Object.values(bundle).find((item) => item.type === 'asset' && item.fileName.endsWith('.css') && String(item.source).includes('.initial-marker'))
    initial.source = css
    return html
  }
}

test('inlines the initial stylesheet while preserving lazy CSS, scripts, and module preloads', async () => {
  await withFixture(async (root) => {
    const baseline = await buildFixture(root, { inline: false })
    const { output, html } = await buildFixture(root)
    assert.match(html, /<style\b[^>]*>[\s\S]*\.initial-marker/)
    assert.doesNotMatch(html, /<link\b[^>]*rel="stylesheet"/)
    assert.doesNotMatch(html.match(/<style\b[^>]*>([\s\S]*?)<\/style>/)[1], /\.lazy-marker/)
    const stylesheets = output.filter((item) => item.type === 'asset' && item.fileName.endsWith('.css'))
    assert.equal(stylesheets.length, 2)
    const lazy = stylesheets.find((item) => String(item.source).includes('.lazy-marker'))
    assert.ok(output.some((item) => item.type === 'chunk' && item.code.includes(lazy.fileName)))
    assert.deepEqual(html.match(/<script\b[^>]*>[\s\S]*?<\/script>/g), baseline.html.match(/<script\b[^>]*>[\s\S]*?<\/script>/g))
    const preloads = /<link\b[^>]*rel="modulepreload"[^>]*>/g
    assert.equal([...baseline.html.matchAll(preloads)].length, 1)
    assert.deepEqual(html.match(preloads), baseline.html.match(preloads))
  })
})

test('uses the 4 KiB UTF-8 boundary and retains oversized stylesheets', async () => {
  await withFixture(async (root) => {
    const prefix = '.initial-marker{--value:"'
    const suffix = '"}'
    for (const [bytes, shouldInline] of [[4096, true], [4097, false]]) {
      const remaining = bytes - Buffer.byteLength(prefix + suffix)
      const css = prefix + '界'.repeat(Math.floor(remaining / 3)) + 'x'.repeat(remaining % 3) + suffix
      assert.equal(Buffer.byteLength(css), bytes)
      const { html } = await buildFixture(root, { beforeInline: replaceInitialCss(css) })
      assert.equal(/<style\b/.test(html), shouldInline, `${bytes} bytes`)
      assert.equal(/<link\b[^>]*rel="stylesheet"/.test(html), !shouldInline, `${bytes} bytes`)
    }
  })
})

test('keeps remote stylesheets and links with attributes that affect stylesheet behavior', async () => {
  await withFixture(async (root) => {
    for (const attribute of ['media="print"', 'disabled', 'integrity="sha384-example"', 'title="optional"', 'data-owner="external"']) {
      const { html } = await buildFixture(root, {
        beforeInline: (html) => html.replace(/(<link rel="stylesheet"[^>]*)(>)/, `$1 ${attribute}$2`),
      })
      assert.doesNotMatch(html, /<style\b/)
      assert.ok(html.includes(attribute))
      assert.match(html, /<link rel="stylesheet"/)
    }
    for (const base of ['https://cdn.example.test/', '//cdn.example.test/']) {
      const { html } = await buildFixture(root, { base })
      assert.doesNotMatch(html, /<style\b/)
      assert.ok(html.includes(`href="${base}assets/`))
    }
  })
})

test('prevents stylesheet text from closing its HTML style element', async () => {
  await withFixture(async (root) => {
    const css = '.initial-marker::before{content:"</StYlE><script>window.injected=true</script>"}'
    const { html } = await buildFixture(root, { beforeInline: replaceInitialCss(css) })
    assert.equal([...html.matchAll(/<style\b/g)].length, 1)
    assert.equal([...html.matchAll(/<\/style\s*>/gi)].length, 1)
    assert.ok(html.includes('<\\/StYlE>'))
    assert.doesNotMatch(html, /<\/StYlE><script>/)
  })
})

test('leaves relative CSS resource URLs and imported stylesheets external', async () => {
  await withFixture(async (root) => {
    for (const css of [
      '.initial-marker{background:url(./image.png)}',
      '.initial-marker{background:image-set("./image.png" 1x)}',
      '@import "./more.css";.initial-marker{color:red}',
    ]) {
      const { html } = await buildFixture(root, { base: './', beforeInline: replaceInitialCss(css) })
      assert.doesNotMatch(html, /<style\b/)
      assert.match(html, /<link rel="stylesheet"/)
    }
  })
})

test('keeps stylesheet-like text in comments and scripts unchanged', async () => {
  await withFixture(async (root) => {
    let literal
    const { html } = await buildFixture(root, {
      beforeInline: (html) => {
        literal = html.match(/<link rel="stylesheet"[^>]*>/)[0]
        return html.replace('</head>', `<!-- ${literal} --><script type="application/json">${JSON.stringify(literal)}</script><iframe srcdoc='${literal}'></iframe></head>`)
      },
    })
    assert.ok(html.includes(`<!-- ${literal} -->`))
    assert.ok(html.includes(`<script type="application/json">${JSON.stringify(literal)}</script>`))
    assert.ok(html.includes(`<iframe srcdoc='${literal}'></iframe>`))
    assert.equal([...html.matchAll(/<style\b/g)].length, 1)
  })
})

test('leaves the development HTML stylesheet loading behavior unchanged', async () => {
  await withFixture(async (root) => {
    const server = await createServer({
      configFile: configPath,
      root,
      logLevel: 'silent',
      server: { middlewareMode: true },
    })
    try {
      const html = await server.transformIndexHtml('/', '<html><head><link rel="stylesheet" href="/entry.css"></head><body></body></html>')
      assert.match(html, /<link rel="stylesheet" href="\/entry.css">/)
      assert.doesNotMatch(html, /<style\b/)
    } finally {
      await server.close()
    }
  })
})
