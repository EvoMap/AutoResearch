import { spawn } from 'node:child_process'
import { resolve } from 'node:path'

function canonicalBlockersCount(
  markdown: string,
  frontmatter: string,
): string | null {
  const declared = frontmatter
    .match(/^\s*blockers(?:[_ -]+)count\s*:\s*(.*?)\s*$/im)?.[1]
    ?.replace(/^['"]|['"]$/g, '')
  if (declared !== undefined && /^\d+$/.test(declared)) return declared
  const findings = (
    markdown.match(/^\s*-\s*\[B\d+\]\s*<severity:[^>\n]+>/gim) || []
  ).length
  if (findings > 0) return String(findings)
  const section = markdown
    .match(/(?:^|\n)##\s+Blockers[^\n]*\n([\s\S]*?)(?=\n##\s+|$)/i)?.[1]
    ?.trim()
  return section && /^(?:-\s*)?none\.?$/i.test(section) ? '0' : null
}

function validateConstraintAudit(markdown: string): void {
  const section = markdown.match(
    /(?:^|\n)##\s+Constraint Audit[^\n]*\n([\s\S]*?)(?=\n##\s+|$)/i,
  )?.[1]
  if (!section) throw new Error('review report missing Constraint Audit')

  const entries = section
    .split(/\r?\n/)
    .filter(line => /^\s*-\s*\[C\d+\]\s+/i.test(line))
  if (entries.length === 0)
    throw new Error('review report Constraint Audit has no entries')
  const constraintIds = entries.map(
    line => line.match(/^\s*-\s*\[(C\d+)\]/i)?.[1].toUpperCase() || '',
  )
  if (new Set(constraintIds).size !== constraintIds.length)
    throw new Error('review report Constraint Audit has duplicate entries')

  const blockerIds = new Set(
    [...markdown.matchAll(/^\s*-\s*\[(B\d+)\]\s*<severity:[^>\n]+>/gim)].map(
      match => match[1].toUpperCase(),
    ),
  )
  const audited = entries.map(line => {
    const status = line.match(
      /\|\s*status:\s*(satisfied|violated|not_verified)\b/i,
    )?.[1]
    if (!status)
      throw new Error('review report Constraint Audit entry has invalid status')
    if (!/\|\s*evidence:\s*\S/i.test(line))
      throw new Error('review report Constraint Audit entry has no evidence')
    const blocker = line.match(/\|\s*blocker:\s*(none|B\d+)\b/i)?.[1]
    if (!blocker)
      throw new Error(
        'review report Constraint Audit entry has invalid blocker',
      )
    return {
      status: status.toLowerCase(),
      blocker: blocker.toUpperCase(),
    }
  })
  const invalidSatisfied = audited.some(
    entry => entry.status === 'satisfied' && entry.blocker !== 'NONE',
  )
  if (invalidSatisfied)
    throw new Error(
      'satisfied hard-constraint audit cannot reference a blocker',
    )
  const missingBlockers = audited.filter(
    entry => entry.status !== 'satisfied' && !blockerIds.has(entry.blocker),
  )
  if (missingBlockers.length > 0)
    throw new Error('hard-constraint audit requires blocker findings')
}

export function attachReviewProvenance(
  markdown: string,
  model: string,
  modelIdentity: string,
  unit?: string,
  cycle?: number,
): string {
  const fenced = markdown
    .trim()
    .match(/^```(?:markdown|md)?\s*\r?\n([\s\S]*?)\r?\n```$/i)
  const normalized = fenced ? fenced[1] : markdown
  let binding = ''
  if (unit !== undefined || cycle !== undefined) {
    const cleanUnit = unit?.trim() || ''
    if (!/^[A-Za-z0-9_.:-]+$/.test(cleanUnit))
      throw new Error(`invalid review unit: ${JSON.stringify(unit)}`)
    if (!Number.isInteger(cycle) || Number(cycle) < 0)
      throw new Error(`invalid review cycle: ${cycle}`)
    binding = `reviewer: gemini-mcp-tool\nunit: ${cleanUnit}\ncycle: ${cycle}\n`
  }
  const metadata = `model: ${model}\nmodel_identity: ${modelIdentity}\n${binding}`
  const frontmatter = normalized.match(/^---\r?\n([\s\S]*?)\r?\n---(?=\r?\n|$)/)
  if (frontmatter) {
    const blockers = canonicalBlockersCount(normalized, frontmatter[1])
    const body = frontmatter[1]
      .split(/\r?\n/)
      .filter(line => {
        if (/^\s*(?:model(?:_identity)?|reviewer|unit|cycle)\s*:/i.test(line))
          return false
        return !(
          blockers !== null && /^\s*blockers(?:[_ -]+)count\s*:/i.test(line)
        )
      })
      .join('\n')
    const count = blockers === null ? '' : `blockers_count: ${blockers}\n`
    const preserved = body ? `${body}\n` : ''
    return `---\n${metadata}${count}${preserved}---${normalized.slice(frontmatter[0].length)}`
  }
  return `---\n${metadata}---\n\n${normalized}`
}

export async function persistReviewReport(
  markdown: string,
  model: string,
  modelIdentity: string,
  unit: string,
  cycle: number,
  projectRoot: string,
  output: string,
): Promise<string> {
  const expected = resolve(projectRoot, 'review.md')
  const target = resolve(output)
  if (target !== expected)
    throw new Error(`review output must be ${expected}; got ${target}`)

  const report = attachReviewProvenance(
    markdown,
    model,
    modelIdentity,
    unit,
    cycle,
  )
  validateConstraintAudit(report)
  await new Promise<void>((resolvePromise, reject) => {
    const engine = resolve(import.meta.dir, 'ar-workflow-engine.py')
    const child = spawn(
      'python3',
      [
        engine,
        'record-review-report',
        '--project-root',
        projectRoot,
        '--unit',
        unit,
        '--cycle',
        String(cycle),
      ],
      { stdio: ['pipe', 'pipe', 'pipe'] },
    )
    let stdout = ''
    let stderr = ''
    child.stdout.setEncoding('utf8').on('data', chunk => (stdout += chunk))
    child.stderr.setEncoding('utf8').on('data', chunk => (stderr += chunk))
    child.on('error', reject)
    child.on('close', code => {
      if (code === 0) resolvePromise()
      else
        reject(
          new Error(
            `workflow engine rejected review report (rc=${code}): ${stdout || stderr}`,
          ),
        )
    })
    child.stdin.end(report)
  })
  return report
}
