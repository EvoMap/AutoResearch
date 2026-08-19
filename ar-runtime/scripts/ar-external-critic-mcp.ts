#!/usr/bin/env bun
import { createHash } from 'node:crypto'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import * as z from 'zod/v4'
import {
  type CriticResult,
  hasIndependentIdentityPair,
  persistCriticReport,
  recordCriticReceipt,
  renderBlindReview,
  requireIndependentCritics,
} from './ar-critic-contract'
import { callRole, checkRole, type RoleCheck } from './ar-role-client'

async function safeCallRole(
  role: string,
  prompt: string,
  excludeIdentities: string[] = [],
): Promise<CriticResult> {
  try {
    const result = await callRole(role, prompt, {
      temperature: 0.2,
      excludeIdentities,
    })
    return {
      role,
      model: result.model,
      modelIdentity: result.model_identity,
      status: 'ok',
      text: result.text,
    }
  } catch (error) {
    return {
      role,
      model: '',
      modelIdentity: '',
      status: 'failed',
      text: '',
      error: String(error),
    }
  }
}

async function safeCallOptionalSecondary(
  prompt: string,
  excludeIdentities: string[] = [],
  existingCheck?: RoleCheck,
): Promise<CriticResult> {
  const check = existingCheck || (await checkRole('critic_secondary'))
  if (check.config && check.ready_models.length === 0) {
    return {
      role: 'critic_secondary',
      model: '',
      modelIdentity: '',
      status: 'skipped',
      text: '',
      error: 'no ready model configured',
    }
  }
  return safeCallRole('critic_secondary', prompt, excludeIdentities)
}

function buildCriticPrompt(params: {
  bundle: string
  context?: string
}): string {
  return `You are an external AutoResearch critic. Challenge whether the research workflow should stop.

Return STRICT markdown with this exact machine-readable header first:

- verdict: finish_ok | needs_revision | needs_more_research
- confidence: high | medium | low
- required_next_focus: <semicolon-separated 0-3 items, or none>
- optional_next_focus: <semicolon-separated 0-3 items, or none>
- stop_reason: <one sentence if verdict=finish_ok, else none>

Then include:
# Critic Review
## Blocking Gaps
## High-ROI Improvements
## Low-ROI / Ignore
## Rationale

Rules:
- Prefer finish_ok only when another iteration is unlikely to change the conclusion.
- Use needs_revision for code, analysis, or reporting defects.
- Use needs_more_research for missing experiments, weak baselines, insufficient evidence, or untested hypotheses.
- Keep required_next_focus concrete and actionable.
- Do not invent missing files; if a file is absent, say which one.

Coordinator context:
${params.context || '(none)'}

AutoResearch bundle:
${params.bundle}
`
}

function extractHeaderValue(text: string, key: string): string {
  return (
    text.match(new RegExp(`^-\\s*${key}:\\s*(.+)$`, 'im'))?.[1]?.trim() || ''
  )
}

function splitFocus(raw: string): string[] {
  if (!raw || /^(none|null|n\/a)$/i.test(raw)) return []
  return raw
    .split(/[;；]/)
    .map(item => item.trim())
    .filter(Boolean)
    .slice(0, 3)
}

function chooseConsensus(results: CriticResult[]): {
  verdict: string
  required: string[]
  optional: string[]
  stopReason: string
} {
  const ok = results.filter(result => result.status === 'ok')
  const verdicts = ok.map(result =>
    extractHeaderValue(result.text, 'verdict').toLowerCase(),
  )
  const required = ok
    .flatMap(result =>
      splitFocus(extractHeaderValue(result.text, 'required_next_focus')),
    )
    .slice(0, 3)
  const optional = ok
    .flatMap(result =>
      splitFocus(extractHeaderValue(result.text, 'optional_next_focus')),
    )
    .slice(0, 3)
  const stopReason =
    ok
      .map(result => extractHeaderValue(result.text, 'stop_reason'))
      .find(Boolean) || 'configured critics agree no high-ROI iteration remains'

  if (verdicts.includes('needs_revision'))
    return { verdict: 'needs_revision', required, optional, stopReason: 'none' }
  if (verdicts.includes('needs_more_research'))
    return {
      verdict: 'needs_more_research',
      required,
      optional,
      stopReason: 'none',
    }
  if (verdicts.includes('finish_ok'))
    return { verdict: 'finish_ok', required: [], optional, stopReason }
  return {
    verdict: 'needs_more_research',
    required: required.length
      ? required
      : ['critic output was unavailable or inconclusive'],
    optional,
    stopReason: 'none',
  }
}

async function runExternalCritic(params: {
  bundle: string
  context?: string
}): Promise<{
  report: string
  results: CriticResult[]
  verdict: string
}> {
  const prompt = buildCriticPrompt(params)
  const primary = await safeCallRole('critic', prompt)
  const secondary = await safeCallOptionalSecondary(
    prompt,
    primary.status === 'ok' ? [primary.modelIdentity] : [],
  )
  const results = [primary, secondary]
  const independent = requireIndependentCritics(results, 'external critic')
  if (
    independent.length !== 2 ||
    results.some(result => result.status !== 'ok')
  )
    throw new Error(
      'external critic requires both configured independent critics to succeed',
    )
  const valid = independent.filter(result =>
    ['finish_ok', 'needs_revision', 'needs_more_research'].includes(
      extractHeaderValue(result.text, 'verdict').toLowerCase(),
    ),
  )
  if (valid.length < independent.length)
    throw new Error(
      'external critic requires every available independent critic to return a parseable verdict',
    )
  const consensus = chooseConsensus(independent)
  return {
    verdict: consensus.verdict,
    results,
    report: `- verdict: ${consensus.verdict}
- confidence: medium
- required_next_focus: ${consensus.required.length ? consensus.required.join('; ') : 'none'}
- optional_next_focus: ${consensus.optional.length ? consensus.optional.join('; ') : 'none'}
- stop_reason: ${consensus.verdict === 'finish_ok' ? consensus.stopReason : 'none'}

# External Critic Consensus

## Role Status
${results.map(result => `- ${result.role}: status=${result.status} model=${result.model || '(none)'} identity=${result.modelIdentity || '(none)'}${result.error ? ` error=${result.error.replace(/\s+/g, ' ').slice(0, 240)}` : ''}`).join('\n')}

${results.map(result => `## ${result.role}\n${result.text || '(unavailable)'}`).join('\n\n')}
`,
  }
}

function buildBlindReviewPrompt(params: {
  submission: string
  venue?: string
  reviewerPersona: string
}): string {
  const venue = params.venue || 'ICLR'
  return `You are Reviewer ${params.reviewerPersona} for ${venue}. You have no prior knowledge of this project. Judge only the submission below.

Return STRICT markdown with this exact machine-readable header first:

- rating: <integer 1-10>
- soundness: <integer 1-4>
- contribution: <integer 1-4>
- presentation: <integer 1-4>
- decision: accept | borderline | reject
- confidence: <integer 1-5>
- top_weaknesses: <semicolon-separated 1-3 items>

Then include:
# Blind Review
## Summary
## Strengths
## Weaknesses
## Questions
## Rationale

Hard rules:
- Ignore self-assessment, predicted scores, and internal review history.
- Calibrate harshly; the median submission is rejected.
- Missing baselines, ablations, significance tests, or verifiable evidence lower the score.

Submission:
${params.submission}
`
}

async function runBlindReview(params: {
  submission: string
  venue?: string
  context?: string
}): Promise<string> {
  const primary = await safeCallRole(
    'critic',
    buildBlindReviewPrompt({
      ...params,
      reviewerPersona: 'A (methods-focused skeptic)',
    }),
  )
  const secondary = await safeCallOptionalSecondary(
    buildBlindReviewPrompt({
      ...params,
      reviewerPersona: 'B (empirical-rigor skeptic)',
    }),
    primary.status === 'ok' ? [primary.modelIdentity] : [],
  )
  const results = [primary, secondary]
  requireIndependentCritics(results, 'blind review')
  return renderBlindReview(results)
}

const server = new McpServer({ name: 'ar-external-critic', version: '2.0.0' })

server.registerTool(
  'blind_review',
  {
    title: 'AutoResearch Blind Review',
    description:
      'Memoryless peer review through the critic roles configured in providers.local.json.',
    inputSchema: {
      submission: z
        .string()
        .min(1)
        .describe('Sanitized paper-style submission without self-scores'),
      venue: z
        .string()
        .optional()
        .describe('Target venue for calibration, default ICLR'),
      context: z
        .string()
        .optional()
        .describe('Optional coordinator context, not shown to reviewers'),
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  async ({ submission, venue, context }) => ({
    content: [
      {
        type: 'text',
        text: await runBlindReview({ submission, venue, context }),
      },
    ],
  }),
)

server.registerTool(
  'external_critic',
  {
    title: 'AutoResearch External Critic',
    description:
      'Challenge whether AutoResearch should finish through configured critic roles.',
    inputSchema: {
      bundle: z
        .string()
        .min(1)
        .describe('Prepared AutoResearch artifact bundle'),
      unit: z
        .string()
        .regex(/^[A-Za-z0-9_.:-]+$/)
        .describe('Workflow critic unit id bound into critic.md'),
      cycle: z
        .number()
        .int()
        .nonnegative()
        .describe('Workflow critic cycle bound into critic.md'),
      project_root: z
        .string()
        .min(1)
        .describe('Absolute AutoResearch project root'),
      output: z
        .string()
        .min(1)
        .describe('critic.md path inside the AutoResearch project root'),
      context: z
        .string()
        .optional()
        .describe('Coordinator context, stage, and close rationale'),
    },
    annotations: { readOnlyHint: false, openWorldHint: true },
  },
  async ({ bundle, unit, cycle, project_root, output, context }) => {
    const result = await runExternalCritic({ bundle, context })
    const report = await persistCriticReport(
      result.report,
      unit,
      cycle,
      project_root,
      output,
    )
    await recordCriticReceipt({
      projectRoot: project_root,
      unit,
      cycle,
      report,
      verdict: result.verdict,
      critics: result.results.map(critic => ({
        role: critic.role,
        model: critic.model,
        modelIdentity: critic.modelIdentity,
        status: critic.status,
        verdict:
          critic.status === 'ok'
            ? extractHeaderValue(critic.text, 'verdict').toLowerCase()
            : '',
        responseSha256:
          critic.status === 'ok'
            ? createHash('sha256').update(critic.text).digest('hex')
            : '',
      })),
    })
    return { content: [{ type: 'text', text: report }] }
  },
)

if (process.argv.includes('--blind-test')) {
  const submission = await new Response(Bun.stdin.stream()).text()
  console.log(await runBlindReview({ submission, venue: 'ICLR' }))
  process.exit(0)
}

if (process.argv.includes('--self-test')) {
  const [criticCheck, secondaryCriticCheck] = await Promise.all([
    checkRole('critic'),
    checkRole('critic_secondary'),
  ])
  const prompt = 'Reply with exactly SELF_TEST_OK.'
  const critic = await safeCallRole('critic', prompt)
  const secondaryCritic = await safeCallOptionalSecondary(
    prompt,
    critic.status === 'ok' ? [critic.modelIdentity] : [],
    secondaryCriticCheck,
  )
  const independentPair = hasIndependentIdentityPair(
    critic.status === 'ok' ? [critic.modelIdentity] : [],
    secondaryCritic.status === 'ok' ? [secondaryCritic.modelIdentity] : [],
  )
  const ok =
    critic.status === 'ok' &&
    secondaryCritic.status === 'ok' &&
    independentPair === true
  console.error(
    JSON.stringify({
      ok,
      server: 'ar-external-critic',
      critic: { check: criticCheck, live_call: critic },
      secondary_critic: {
        check: secondaryCriticCheck,
        live_call: secondaryCritic,
      },
      independent_pair: independentPair,
    }),
  )
  process.exit(ok ? 0 : 1)
}

await server.connect(new StdioServerTransport())
