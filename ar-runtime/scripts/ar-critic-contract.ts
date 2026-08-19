import { createHash, randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { rename, rm, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'

export type CriticResult = {
  role: string
  model: string
  modelIdentity: string
  status: 'ok' | 'failed' | 'skipped'
  text: string
  error?: string
}

export const MIN_INDEPENDENT_CRITICS = 2

export type CriticReceiptResult = {
  role: string
  model: string
  modelIdentity: string
  status: 'ok' | 'failed' | 'skipped'
  verdict: string
  responseSha256: string
}

export function bindCriticReport(
  text: string,
  unit: string,
  cycle: number,
): string {
  const cleanUnit = unit.trim()
  if (!/^[A-Za-z0-9_.:-]+$/.test(cleanUnit))
    throw new Error(`invalid critic unit: ${JSON.stringify(unit)}`)
  if (!Number.isInteger(cycle) || cycle < 0)
    throw new Error(`invalid critic cycle: ${cycle}`)
  return `- unit: ${cleanUnit}\n- cycle: ${cycle}\n${text.trimStart()}`
}

export async function persistCriticReport(
  text: string,
  unit: string,
  cycle: number,
  projectRoot: string,
  output: string,
): Promise<string> {
  const expected = resolve(projectRoot, 'critic.md')
  const target = resolve(output)
  if (target !== expected)
    throw new Error(`critic output must be ${expected}; got ${target}`)

  const report = bindCriticReport(text, unit, cycle)
  const temporary = resolve(
    projectRoot,
    `.critic.md.${process.pid}.${randomUUID()}.tmp`,
  )
  try {
    await writeFile(temporary, report, { encoding: 'utf8', mode: 0o600 })
    await rename(temporary, target)
  } finally {
    await rm(temporary, { force: true })
  }
  return report
}

function enginePython(): string {
  if (process.env.AR_ENGINE_PYTHON) return process.env.AR_ENGINE_PYTHON
  const repoRoot = resolve(import.meta.dir, '../..')
  const bundled =
    process.platform === 'win32'
      ? resolve(repoRoot, '.venv', 'Scripts', 'python.exe')
      : resolve(repoRoot, '.venv', 'bin', 'python')
  return existsSync(bundled)
    ? bundled
    : process.platform === 'win32'
      ? 'python'
      : 'python3'
}

export async function recordCriticReceipt(params: {
  projectRoot: string
  unit: string
  cycle: number
  report: string
  verdict: string
  critics: CriticReceiptResult[]
}): Promise<string> {
  const engine = resolve(import.meta.dir, 'ar-workflow-engine.py')
  const payload = {
    schema_version: 1,
    request_id: randomUUID(),
    issued_at: new Date().toISOString(),
    producer_pid: process.pid,
    unit: params.unit,
    cycle: params.cycle,
    artifact: 'critic.md',
    artifact_sha256: createHash('sha256').update(params.report).digest('hex'),
    verdict: params.verdict,
    critics: params.critics.map(result => ({
      role: result.role,
      model: result.model,
      model_identity: result.modelIdentity,
      status: result.status,
      verdict: result.verdict || null,
      response_sha256: result.responseSha256 || null,
    })),
  }
  const child = Bun.spawn(
    [
      enginePython(),
      engine,
      'record-critic-receipt',
      '--project-root',
      resolve(params.projectRoot),
    ],
    {
      cwd: resolve(import.meta.dir, '../..'),
      env: process.env,
      stdin: 'pipe',
      stdout: 'pipe',
      stderr: 'pipe',
    },
  )
  const stdin = child.stdin
  if (!stdin) throw new Error('critic receipt engine stdin was not opened')
  stdin.write(JSON.stringify(payload))
  stdin.end()
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ])
  if (exitCode !== 0)
    throw new Error(
      `critic receipt engine rejected producer evidence (${exitCode}): ${stderr.trim() || stdout.trim()}`,
    )
  return stdout.trim()
}

export function hasIndependentIdentityPair(
  primary: string[] = [],
  secondary: string[] = [],
): boolean {
  const selectedPrimary = primary
    .map(identity => identity.trim().toLowerCase())
    .find(Boolean)
  if (!selectedPrimary) return false
  return secondary.some(identity => {
    const normalized = identity.trim().toLowerCase()
    return normalized !== '' && normalized !== selectedPrimary
  })
}

export function independentCriticResults(
  results: CriticResult[],
): CriticResult[] {
  const identities = new Set<string>()
  return results.filter(result => {
    if (result.status !== 'ok') return false
    const identity = result.modelIdentity.trim().toLowerCase()
    if (!identity || identities.has(identity)) return false
    identities.add(identity)
    return true
  })
}

export function requireIndependentCritics(
  results: CriticResult[],
  label: string,
): CriticResult[] {
  const required = results.some(
    result => result.role === 'critic_secondary' && result.status === 'skipped',
  )
    ? 1
    : MIN_INDEPENDENT_CRITICS
  const independent = independentCriticResults(results)
  if (independent.length >= required) return independent
  const status = results
    .map(
      result =>
        `${result.role}=${result.status}:${result.modelIdentity || result.model || 'none'}`,
    )
    .join(', ')
  throw new Error(
    `${label} requires ${required} independent models; got ${independent.length} (${status})`,
  )
}

function header(text: string, key: string): string {
  return (
    text.match(new RegExp(`^-\\s*${key}:\\s*(.+)$`, 'im'))?.[1]?.trim() || ''
  )
}

function items(raw: string): string[] {
  if (!raw || /^(none|null|n\/a)$/i.test(raw)) return []
  return raw
    .split(/[;；]/)
    .map(item => item.trim())
    .filter(Boolean)
}

export function renderBlindReview(results: CriticResult[]): string {
  const required = results.some(
    result => result.role === 'critic_secondary' && result.status === 'skipped',
  )
    ? 1
    : MIN_INDEPENDENT_CRITICS
  const independent = independentCriticResults(results)
  const ratings = independent
    .map(result => Number.parseFloat(header(result.text, 'rating')))
    .filter(Number.isFinite)
  const average = ratings.length
    ? ratings.reduce((total, rating) => total + rating, 0) / ratings.length
    : 0
  const decisions = independent.map(result =>
    header(result.text, 'decision').toLowerCase(),
  )
  const decision =
    ratings.length < required
      ? 'unavailable'
      : decisions.includes('reject') && average < 5.5
        ? 'reject'
        : average >= 6
          ? 'accept'
          : 'borderline'
  const weaknesses = independent
    .flatMap(result => items(header(result.text, 'top_weaknesses')))
    .slice(0, 4)

  return `- avg_rating: ${ratings.length ? average.toFixed(1) : 'none'}
- n_reviews: ${ratings.length}
- decision: ${decision}
- top_weaknesses: ${weaknesses.length ? weaknesses.join('; ') : 'none'}

# Blind Review Panel (memoryless)

## Role Status
${results.map(result => `- ${result.role}: status=${result.status} model=${result.model || '(none)'} identity=${result.modelIdentity || '(none)'}${result.error ? ` error=${result.error.replace(/\s+/g, ' ').slice(0, 240)}` : ''}`).join('\n')}

${results.map((result, index) => `## Reviewer ${index === 0 ? 'A' : 'B'}\n${result.text || `(unavailable: ${result.error || 'unknown error'})`}`).join('\n\n')}
`
}

if (import.meta.main) {
  const results = (await Bun.stdin.json()) as CriticResult[]
  console.log(renderBlindReview(results))
}
