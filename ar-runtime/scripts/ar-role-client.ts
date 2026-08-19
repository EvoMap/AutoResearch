import { existsSync } from 'node:fs'
import path from 'node:path'

export type RoleReply = {
  role: string
  configured_models: string[]
  model: string
  model_identity: string
  text: string
}

export type RoleCheck = {
  ok: boolean
  role: string
  configured_models: string[]
  ready_models: string[]
  ready_model_identities?: string[]
  config: string
  error?: string
}

const repoRoot = path.resolve(import.meta.dir, '../..')
const bridge = path.join(repoRoot, 'scripts', 'call_role.py')

class RoleBridgeError extends Error {
  constructor(
    readonly stdout: string,
    readonly stderr: string,
    readonly exitCode: number,
  ) {
    super(stderr.trim() || stdout.trim() || `role bridge exited ${exitCode}`)
  }
}

export function roleCheckFromFailure(
  role: string,
  stdout: string,
  stderr: string,
  exitCode: number,
): RoleCheck {
  try {
    const report = JSON.parse(stdout.trim()) as Partial<RoleCheck>
    if (
      report.role === role &&
      Array.isArray(report.configured_models) &&
      Array.isArray(report.ready_models) &&
      typeof report.config === 'string'
    ) {
      return {
        ok: false,
        role,
        configured_models: report.configured_models,
        ready_models: report.ready_models,
        ...(Array.isArray(report.ready_model_identities)
          ? { ready_model_identities: report.ready_model_identities }
          : {}),
        config: report.config,
        error: `role bridge exited ${exitCode}`,
      }
    }
  } catch {
    // Fall through to the textual process error when the bridge did not emit its contract.
  }
  return {
    ok: false,
    role,
    configured_models: [],
    ready_models: [],
    config: '',
    error: stderr.trim() || stdout.trim() || `role bridge exited ${exitCode}`,
  }
}

function pythonCommand(): string {
  if (process.env.AUTORESEARCH_PYTHON) return process.env.AUTORESEARCH_PYTHON
  const candidates =
    process.platform === 'win32'
      ? [path.join(repoRoot, '.venv', 'Scripts', 'python.exe'), 'python']
      : [path.join(repoRoot, '.venv', 'bin', 'python'), 'python3', 'python']
  return (
    candidates.find(
      candidate => !candidate.includes(path.sep) || existsSync(candidate),
    ) || candidates.at(-1)!
  )
}

async function runBridge(args: string[], input?: unknown): Promise<string> {
  const child = Bun.spawn([pythonCommand(), bridge, ...args], {
    cwd: repoRoot,
    env: process.env,
    stdin: input === undefined ? 'ignore' : 'pipe',
    stdout: 'pipe',
    stderr: 'pipe',
  })
  if (input !== undefined) {
    const stdin = child.stdin
    if (!stdin) throw new Error('role bridge stdin was not opened')
    stdin.write(JSON.stringify(input))
    stdin.end()
  }
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ])
  if (exitCode !== 0) {
    throw new RoleBridgeError(stdout, stderr, exitCode)
  }
  return stdout.trim()
}

export async function callRole(
  role: string,
  prompt: string,
  options: {
    temperature?: number
    maxTokens?: number
    system?: string
    excludeIdentities?: string[]
  } = {},
): Promise<RoleReply> {
  return JSON.parse(
    await runBridge(['--role', role], {
      prompt,
      temperature: options.temperature,
      max_tokens: options.maxTokens,
      system: options.system,
      exclude_identities: options.excludeIdentities,
    }),
  ) as RoleReply
}

export async function checkRole(role: string): Promise<RoleCheck> {
  try {
    return JSON.parse(
      await runBridge(['--role', role, '--self-test']),
    ) as RoleCheck
  } catch (error) {
    if (error instanceof RoleBridgeError) {
      return roleCheckFromFailure(
        role,
        error.stdout,
        error.stderr,
        error.exitCode,
      )
    }
    return roleCheckFromFailure(role, '', String(error), 1)
  }
}
