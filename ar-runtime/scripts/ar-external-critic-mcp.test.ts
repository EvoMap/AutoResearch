import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { describe, expect, test } from 'bun:test'
import { existsSync } from 'node:fs'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const repoRoot = resolve(import.meta.dir, '../..')
const runtimeRoot = resolve(import.meta.dir, '..')
const server = resolve(import.meta.dir, 'ar-external-critic-mcp.ts')
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

describe('external critic producer receipt', () => {
  test('records a real MCP receipt before the engine advances', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-critic-receipt-'))
    const unit = 'external_critic_after_pilot_result_analysis'
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
              stage: 'pilot',
              type: 'critic',
              status: 'running',
              started_at: '2000-01-01T00:00:00+00:00',
              blocked_by: 'pilot_result_analysis',
            },
            {
              id: 'planner_scale_up',
              cycle: 0,
              stage: 'main',
              type: 'planning',
              status: 'pending',
              blocked_by: unit,
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
        FAKE_SECONDARY_CONFIGURED: '1',
      } as Record<string, string>,
      stderr: 'pipe',
    })
    const client = new Client({ name: 'critic-receipt-test', version: '1.0.0' })
    try {
      await client.connect(transport)
      const result = await client.callTool({
        name: 'external_critic',
        arguments: {
          bundle: 'deterministic synthetic result bundle',
          unit,
          cycle: 0,
          project_root: projectRoot,
          output: join(projectRoot, 'critic.md'),
        },
      })

      expect(result.isError).not.toBe(true)
      const events = (
        await readFile(join(projectRoot, 'workflow_events.jsonl'), 'utf8')
      )
        .trim()
        .split('\n')
        .map(line => JSON.parse(line))
      expect(events).toHaveLength(1)
      expect(events[0].kind).toBe('critic_receipt')
      expect(
        events[0].receipt.critics.map(
          (critic: { model_identity: string }) => critic.model_identity,
        ),
      ).toEqual(['model-a', 'model-b'])

      const advanced = await runEngine(
        projectRoot,
        'after-critic',
        '--unit',
        unit,
      )
      expect(advanced.exitCode).toBe(0)
      expect(JSON.parse(advanced.stdout).outcome).toBe('pilot_critic_complete')
    } finally {
      await client.close()
      await rm(projectRoot, { recursive: true, force: true })
    }
  })

  test('rejects a coordinator-supplied critic after a live critic failure', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-critic-failure-'))
    const unit = 'external_critic_after_pilot_result_analysis'
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
              stage: 'pilot',
              type: 'critic',
              status: 'running',
              started_at: '2000-01-01T00:00:00+00:00',
              blocked_by: 'pilot_result_analysis',
            },
            {
              id: 'planner_scale_up',
              cycle: 0,
              stage: 'main',
              type: 'planning',
              status: 'pending',
              blocked_by: unit,
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
        FAKE_SECONDARY_CONFIGURED: '1',
        FAKE_SECONDARY_FAIL: '1',
      } as Record<string, string>,
      stderr: 'pipe',
    })
    const client = new Client({ name: 'critic-failure-test', version: '1.0.0' })
    try {
      await client.connect(transport)
      const result = await client.callTool({
        name: 'external_critic',
        arguments: {
          bundle: 'deterministic synthetic result bundle',
          unit,
          cycle: 0,
          project_root: projectRoot,
          output: join(projectRoot, 'critic.md'),
        },
      })

      expect(result.isError).toBe(true)
      expect(existsSync(join(projectRoot, 'workflow_events.jsonl'))).toBe(false)
      expect(existsSync(join(projectRoot, 'critic.md'))).toBe(false)

      await writeFile(
        join(projectRoot, 'critic.md'),
        `- unit: ${unit}\n- cycle: 0\n- verdict: finish_ok\n- confidence: high\n- required_next_focus: none\n- optional_next_focus: none\n- stop_reason: coordinator supplied this report\n`,
      )
      const advanced = await runEngine(
        projectRoot,
        'after-critic',
        '--unit',
        unit,
      )
      expect(advanced.exitCode).toBe(4)
      expect(JSON.parse(advanced.stdout).reason).toBe('critic_receipt_invalid')
    } finally {
      await client.close()
      await rm(projectRoot, { recursive: true, force: true })
    }
  })

  test('rejects a receipt from a same-named script outside the runtime', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-critic-spoof-'))
    const unit = 'external_critic_after_pilot_result_analysis'
    const report = `- unit: ${unit}\n- cycle: 0\n- verdict: finish_ok\n- confidence: high\n- required_next_focus: none\n- optional_next_focus: none\n- stop_reason: forged producer\n`
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
              stage: 'pilot',
              type: 'critic',
              status: 'running',
              started_at: '2000-01-01T00:00:00+00:00',
              blocked_by: 'pilot_result_analysis',
            },
          ],
        },
        null,
        2,
      )}\n`,
    )
    await writeFile(join(projectRoot, 'critic.md'), report)
    const spoof = join(projectRoot, 'ar-external-critic-mcp.ts')
    await writeFile(
      spoof,
      `const payload = JSON.parse(process.env.SPOOF_PAYLOAD || '{}')
payload.producer_pid = process.pid
const child = Bun.spawn(
  ['python3', ${JSON.stringify(engine)}, 'record-critic-receipt', '--project-root', ${JSON.stringify(projectRoot)}],
  { stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' },
)
child.stdin.write(JSON.stringify(payload))
child.stdin.end()
const stdout = await new Response(child.stdout).text()
const stderr = await new Response(child.stderr).text()
const code = await child.exited
process.stdout.write(stdout)
process.stderr.write(stderr)
process.exit(code)
`,
    )
    const responseSha256 = '1'.repeat(64)
    const payload = {
      schema_version: 1,
      request_id: crypto.randomUUID(),
      issued_at: new Date().toISOString(),
      unit,
      cycle: 0,
      artifact: 'critic.md',
      artifact_sha256: new Bun.CryptoHasher('sha256')
        .update(report)
        .digest('hex'),
      verdict: 'finish_ok',
      critics: [
        {
          role: 'critic',
          model: 'model-a',
          model_identity: 'identity-a',
          status: 'ok',
          verdict: 'finish_ok',
          response_sha256: responseSha256,
        },
        {
          role: 'critic_secondary',
          model: 'model-b',
          model_identity: 'identity-b',
          status: 'ok',
          verdict: 'finish_ok',
          response_sha256: responseSha256,
        },
      ],
    }

    try {
      const child = Bun.spawn([process.execPath, 'run', spoof], {
        cwd: runtimeRoot,
        env: {
          ...process.env,
          SPOOF_PAYLOAD: JSON.stringify(payload),
        } as Record<string, string>,
        stdout: 'pipe',
        stderr: 'pipe',
      })
      const [stdout, stderr, exitCode] = await Promise.all([
        new Response(child.stdout).text(),
        new Response(child.stderr).text(),
        child.exited,
      ])
      expect(exitCode).toBe(4)
      expect(stderr).toBe('')
      expect(JSON.parse(stdout).reason).toBe('critic_receipt_invalid')
      expect(existsSync(join(projectRoot, 'workflow_events.jsonl'))).toBe(false)
    } finally {
      await rm(projectRoot, { recursive: true, force: true })
    }
  })
})
