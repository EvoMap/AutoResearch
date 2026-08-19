import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { describe, expect, test } from 'bun:test'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const repoRoot = resolve(import.meta.dir, '../..')
const runtimeRoot = resolve(import.meta.dir, '..')
const server = resolve(import.meta.dir, 'ar-gemini-review-mcp.ts')
const engine = resolve(import.meta.dir, 'ar-workflow-engine.py')
const fakeBridge = resolve(
  import.meta.dir,
  'tests',
  'fixtures',
  'fake_role_bridge.py',
)

async function runEngine(projectRoot: string, ...args: string[]) {
  const child = Bun.spawn(
    ['python3', engine, ...args, '--project-root', projectRoot],
    { cwd: repoRoot, stdout: 'pipe', stderr: 'pipe' },
  )
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ])
  return { stdout, stderr, exitCode }
}

describe('review producer receipt', () => {
  test('makes every hard constraint an auditable blocker condition', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-review-prompt-'))
    const promptLog = join(projectRoot, 'review-prompt.txt')
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ['run', server],
      cwd: runtimeRoot,
      env: {
        ...process.env,
        AUTORESEARCH_PYTHON: fakeBridge,
        FAKE_ROLE_PROMPT_LOG: promptLog,
      } as Record<string, string>,
      stderr: 'pipe',
    })
    const client = new Client({ name: 'review-prompt-test', version: '1.0.0' })
    try {
      await client.connect(transport)
      const result = await client.callTool({
        name: 'gemini_review',
        arguments: {
          code: 'values = generate_sequence(n, seed + repetition)',
          context:
            'Hard constraint: use seed 1729 unchanged for exactly two repetitions.',
        },
      })

      expect(result.isError).not.toBe(true)
      const prompt = await readFile(promptLog, 'utf8')
      expect(prompt).toContain('## Constraint Audit')
      expect(prompt).toContain('status: satisfied|violated|not_verified')
      expect(prompt).toContain(
        'Every hard constraint from Context must appear in the Constraint Audit exactly once.',
      )
      expect(prompt).toContain(
        'Any violated or not_verified constraint must also be reported as a blocker.',
      )
      expect(prompt).toContain(
        'Trace literal parameter values through every repetition; changing a fixed seed',
      )
      expect(prompt).toContain(
        'Warnings are limited to non-contract improvements',
      )
    } finally {
      await client.close()
      await rm(projectRoot, { recursive: true, force: true })
    }
  })

  test('records a real MCP receipt before the engine completes the review', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-review-receipt-'))
    const requestLog = join(projectRoot, 'role-requests.jsonl')
    const unit = 'review_iteration_c1'
    await writeFile(
      join(projectRoot, 'workflow_queue.json'),
      `${JSON.stringify(
        {
          mode: 'autoresearch_loop',
          schema_version: 1,
          completion_authority_version: 1,
          current_cycle: 1,
          max_cycles: 1,
          units: [
            {
              id: unit,
              cycle: 1,
              type: 'review',
              status: 'running',
              started_at: '2000-01-01T00:00:00+00:00',
            },
          ],
        },
        null,
        2,
      )}\n`,
    )
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ['run', server],
      cwd: runtimeRoot,
      env: {
        ...process.env,
        AUTORESEARCH_PYTHON: fakeBridge,
        FAKE_ROLE_REQUEST_LOG: requestLog,
      } as Record<string, string>,
      stderr: 'pipe',
    })
    const client = new Client({ name: 'review-receipt-test', version: '1.0.0' })
    try {
      await client.connect(transport)
      const result = await client.callTool({
        name: 'gemini_review',
        arguments: {
          code: 'def answer(): return 42',
          unit,
          cycle: 1,
          project_root: projectRoot,
          output: join(projectRoot, 'review.md'),
        },
      })

      expect(result.isError).not.toBe(true)
      const requests = (await readFile(requestLog, 'utf8'))
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
      expect(requests).toEqual([{ role: 'code_reviewer', max_tokens: 4096 }])
      const report = await readFile(join(projectRoot, 'review.md'), 'utf8')
      expect(report).toStartWith('---\nmodel: code_reviewer-model\n')
      expect(report).not.toContain('```markdown')
      const events = (
        await readFile(join(projectRoot, 'workflow_events.jsonl'), 'utf8')
      )
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
      expect(events).toHaveLength(1)
      expect(events[0].kind).toBe('review_report_receipt')
      expect(events[0].source).toBe('ar-gemini-review-mcp')
      expect(events[0].receipt.model_identity).toBe('model-a')

      const completed = await runEngine(
        projectRoot,
        'complete',
        '--unit',
        unit,
        '--status',
        'done',
      )
      expect(completed.exitCode).toBe(0)
      const terminal = (
        await readFile(join(projectRoot, 'workflow_events.jsonl'), 'utf8')
      )
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
        .at(-1)
      expect(terminal.kind).toBe('unit_terminal')
      expect(terminal.evidence.producer_receipt.event_hash).toBe(
        events[0].event_hash,
      )
    } finally {
      await client.close()
      await rm(projectRoot, { recursive: true, force: true })
    }
  })

  test('normalizes a copied count placeholder when the blockers section is empty', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-review-count-'))
    const unit = 'code_review'
    await writeFile(
      join(projectRoot, 'workflow_queue.json'),
      `${JSON.stringify(
        {
          mode: 'autoresearch_loop',
          schema_version: 1,
          completion_authority_version: 1,
          current_cycle: 0,
          max_cycles: 1,
          units: [
            {
              id: unit,
              cycle: 0,
              type: 'review',
              status: 'running',
              started_at: '2000-01-01T00:00:00+00:00',
            },
          ],
        },
        null,
        2,
      )}\n`,
    )
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ['run', server],
      cwd: runtimeRoot,
      env: {
        ...process.env,
        AUTORESEARCH_PYTHON: fakeBridge,
        FAKE_REVIEW_BLOCKERS_COUNT: '<int>',
      } as Record<string, string>,
      stderr: 'pipe',
    })
    const client = new Client({ name: 'review-count-test', version: '1.0.0' })
    try {
      await client.connect(transport)
      const result = await client.callTool({
        name: 'gemini_review',
        arguments: {
          code: 'def answer(): return 42',
          unit,
          cycle: 0,
          project_root: projectRoot,
          output: join(projectRoot, 'review.md'),
        },
      })

      expect(result.isError).not.toBe(true)
      const report = await readFile(join(projectRoot, 'review.md'), 'utf8')
      expect(report).toContain('\nblockers_count: 0\n')
      expect(report).not.toContain('blockers_count: <int>')
      const events = (
        await readFile(join(projectRoot, 'workflow_events.jsonl'), 'utf8')
      )
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
      expect(events.at(-1).receipt.blockers_count).toBe(0)
    } finally {
      await client.close()
      await rm(projectRoot, { recursive: true, force: true })
    }
  })
})
