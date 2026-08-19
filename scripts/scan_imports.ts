#!/usr/bin/env bun
/**
 * Emit every import in a TypeScript tree as JSON, using Bun's own parser.
 *
 * A regular expression cannot do this correctly. It misses path-alias specifiers,
 * cannot tell a type-only import from a value one, and matches text inside comments
 * and string literals. Bun.Transpiler.scanImports is the parser the runtime already
 * uses, so what it reports is what actually gets resolved.
 *
 * Output: one JSON object per line, {file, specifier, kind}, where kind is one of
 * import-statement, require-call or dynamic-import.
 *
 * Usage: bun run scripts/scan_imports.ts <dir> [<dir> ...]
 */
import { Glob } from 'bun'
import { join, extname } from 'path'

const LOADERS: Record<string, 'ts' | 'tsx' | 'js' | 'jsx'> = {
  '.ts': 'ts',
  '.tsx': 'tsx',
  '.mts': 'ts',
  '.cts': 'ts',
  '.js': 'js',
  '.jsx': 'jsx',
  '.mjs': 'js',
  '.cjs': 'js',
}

const SKIP = ['node_modules', 'dist', 'build', '.git']

const roots = process.argv.slice(2)
if (roots.length === 0) {
  console.error('usage: bun run scripts/scan_imports.ts <dir> [<dir> ...]')
  process.exit(2)
}

// One transpiler per loader; constructing per file is measurably slower on a tree
// this size.
const transpilers = new Map<string, Bun.Transpiler>()
function transpilerFor(loader: 'ts' | 'tsx' | 'js' | 'jsx'): Bun.Transpiler {
  let existing = transpilers.get(loader)
  if (!existing) {
    existing = new Bun.Transpiler({ loader })
    transpilers.set(loader, existing)
  }
  return existing
}

let scanned = 0
let failed = 0

for (const root of roots) {
  const glob = new Glob('**/*.{ts,tsx,mts,cts,js,jsx,mjs,cjs}')
  for await (const relative of glob.scan({ cwd: root, onlyFiles: true, dot: false })) {
    if (SKIP.some(part => relative.split('/').includes(part))) continue

    const file = join(root, relative)
    const loader = LOADERS[extname(relative)]
    if (!loader) continue

    let source: string
    try {
      source = await Bun.file(file).text()
    } catch {
      continue
    }

    // The transpiler rejects a shebang line; executables carry one and still have
    // imports worth checking.
    if (source.startsWith('#!')) {
      const newline = source.indexOf('\n')
      source = newline === -1 ? '' : source.slice(newline + 1)
    }

    scanned += 1
    // Some .js files in this tree hold TypeScript syntax, so a failure under the
    // extension's own loader is retried as TS before being called unparseable.
    const attempts: Array<'ts' | 'tsx' | 'js' | 'jsx'> =
      loader === 'js' || loader === 'jsx' ? [loader, 'tsx'] : [loader]

    let parsed = false
    let lastError: unknown
    for (const attempt of attempts) {
      try {
        for (const imported of transpilerFor(attempt).scanImports(source)) {
          console.log(JSON.stringify({ file, specifier: imported.path, kind: imported.kind }))
        }
        parsed = true
        break
      } catch (error) {
        lastError = error
      }
    }

    if (!parsed) {
      // Reported rather than skipped: a file the parser rejects would otherwise look
      // like a file with no dependencies at all.
      failed += 1
      console.error(`parse failed: ${file}: ${lastError}`)
    }
  }
}

console.error(`scanned ${scanned} files, ${failed} unparseable`)
if (failed > 0) process.exit(1)
