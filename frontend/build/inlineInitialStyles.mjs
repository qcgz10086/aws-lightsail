import { Buffer } from 'node:buffer'

const maxInlineBytes = 4 * 1024
const htmlParts = /<!--[\s\S]*?(?:-->|$)|<(script|style|textarea|title)\b(?:[^"'<>]|"[^"]*"|'[^']*')*>[\s\S]*?(?:<\/\1\s*>|$)|<\/?[a-z][\w:-]*\b(?:[^"'<>]|"[^"]*"|'[^']*')*>/gi
const attribute = /\s+([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/gy

function stylesheetHref(tag) {
  const text = tag.slice(5, -1).replace(/\/\s*$/, '')
  const attributes = new Map()
  let position = 0
  while (position < text.length) {
    if (!text.slice(position).trim()) break
    attribute.lastIndex = position
    const match = attribute.exec(text)
    if (!match) return
    const name = match[1].toLowerCase()
    if (!['rel', 'href', 'crossorigin'].includes(name) || attributes.has(name)) return
    attributes.set(name, match[2] ?? match[3] ?? match[4] ?? '')
    position = attribute.lastIndex
  }
  if (attributes.get('rel')?.toLowerCase() !== 'stylesheet') return
  if (attributes.has('crossorigin') && !['', 'anonymous'].includes(attributes.get('crossorigin').toLowerCase())) return
  return attributes.get('href')
}

function bundleFilename(href, htmlPath, base) {
  if (!href || /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(href) || /[?#&\\]/.test(href)) return
  try {
    const pathname = decodeURIComponent(new URL(href, `https://inline.invalid${htmlPath}`).pathname)
    const basePath = base.startsWith('/') ? base : '/'
    if (!pathname.startsWith(basePath)) return
    return pathname.slice(basePath.length)
  } catch {
    return
  }
}

function hasRelativeResources(css) {
  // 外部样式表中的相对资源以 CSS 文件为基准，内联后会改为 HTML 地址。
  if (/@import\b|\b(?:-webkit-)?image-set\s*\(/i.test(css)) return true
  return [...css.matchAll(/\burl\(\s*["']?([^\s"')]+)/gi)].some((match) => !/^(?:[a-z][a-z\d+.-]*:|\/)/i.test(match[1]))
}

/** @returns {import('vite').Plugin} */
export function inlineInitialStyles() {
  let base = '/'
  return {
    name: 'inline-initial-styles',
    apply: 'build',
    configResolved(config) {
      base = config.base
    },
    transformIndexHtml: {
      order: 'post',
      handler(html, context) {
        if (!context.bundle) return html
        return html.replace(htmlParts, (tag, rawElement) => {
          if (rawElement || !/^<link\b/i.test(tag)) return tag
          const fileName = bundleFilename(stylesheetHref(tag), context.path, base)
          const asset = fileName && context.bundle[fileName]
          if (!asset || asset.type !== 'asset' || !fileName.endsWith('.css')) return tag
          const css = typeof asset.source === 'string' ? asset.source : new TextDecoder().decode(asset.source)
          if (Buffer.byteLength(css) > maxInlineBytes || hasRelativeResources(css)) return tag
          // 保留 CSS 产物，供 Vite 的懒加载和共享样式依赖继续引用。
          return `<style>${css.replace(/<\/style/gi, (closing) => `<\\/${closing.slice(2)}`)}</style>`
        })
      },
    },
  }
}
