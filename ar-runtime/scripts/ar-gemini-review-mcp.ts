#!/usr/bin/env bun
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import * as z from 'zod/v4'
import { callRole, checkRole } from './ar-role-client'
import {
  attachReviewProvenance,
  persistReviewReport,
} from './ar-review-contract'

function buildReviewPrompt(params: { code: string; context?: string }): string {
  return `You are a senior research engineer reviewing experiment code.
Return STRICT markdown with this exact structure:

---
blockers_count: <int>
warnings_count: <int>
files_reviewed: <int>
reviewer_role: code_reviewer
---

# Review

## Blockers (must fix before running)
- [B1] <severity:high> <file>:<line>: <issue> | impact: <one line>

## Warnings (should fix)
- [W1] <severity:med> <file>:<line>: <issue>

## Constraint Audit
- [C1] <hard constraint from Context> | status: satisfied|violated|not_verified | evidence: <file:line and traced value/control flow> | blocker: none|B1

## Notes
- <low risk observation>

## Overall
<3-5 sentences>

Rules:
- Only flag real issues. Do not manufacture findings.
- Blocker means crash, silent wrong results, data leakage, secret leakage, invalid experiment conclusion, or any violated or unverified hard constraint.
- Every hard constraint from Context must appear in the Constraint Audit exactly once.
- Any violated or not_verified constraint must also be reported as a blocker. Its audit entry must reference that B-number; satisfied entries use blocker: none.
- Trace literal parameter values through every repetition; changing a fixed seed, size, count, stage, or path is a violation even when totals still match.
- Warnings are limited to non-contract improvements that cannot change experiment scope, required values, or validity.
- If a section has no findings, write "None".
- Keep findings concrete and file/line grounded when possible.

Context:
${params.context || '(none)'}

Code bundle:
${params.code}
`
}

const server = new McpServer({ name: 'ar-gemini-review', version: '2.0.0' })

server.registerTool(
  'gemini_review',
  {
    title: 'AutoResearch Code Review',
    description:
      'Compatibility tool name: review through the code_reviewer role configured in providers.local.json.',
    inputSchema: {
      code: z.string().min(1).describe('Prepared code bundle to review'),
      context: z
        .string()
        .optional()
        .describe('Plan, success criteria, constraints, or reviewer notes'),
      temperature: z.number().min(0).max(2).optional(),
      max_output_tokens: z.number().int().positive().optional(),
      unit: z
        .string()
        .regex(/^[A-Za-z0-9_.:-]+$/)
        .optional(),
      cycle: z.number().int().nonnegative().optional(),
      project_root: z.string().min(1).optional(),
      output: z.string().min(1).optional(),
    },
    annotations: { readOnlyHint: false, openWorldHint: true },
  },
  async ({
    code,
    context,
    temperature,
    max_output_tokens,
    unit,
    cycle,
    project_root,
    output,
  }) => {
    const result = await callRole(
      'code_reviewer',
      buildReviewPrompt({ code, context }),
      {
        temperature: temperature ?? 0.2,
        maxTokens: max_output_tokens ?? 4096,
      },
    )
    const persistenceRequested =
      unit !== undefined ||
      cycle !== undefined ||
      project_root !== undefined ||
      output !== undefined
    if (
      persistenceRequested &&
      (unit === undefined ||
        cycle === undefined ||
        project_root === undefined ||
        output === undefined)
    )
      throw new Error(
        'code review persistence requires unit, cycle, project_root, and output',
      )
    const text = persistenceRequested
      ? await persistReviewReport(
          result.text,
          result.model,
          result.model_identity,
          unit as string,
          cycle as number,
          project_root as string,
          output as string,
        )
      : attachReviewProvenance(result.text, result.model, result.model_identity)
    return {
      content: [
        {
          type: 'text',
          text,
        },
      ],
    }
  },
)

if (process.argv.includes('--self-test')) {
  const check = await checkRole('code_reviewer')
  console.error(JSON.stringify({ server: 'ar-gemini-review', ...check }))
  process.exit(check.ok ? 0 : 1)
}

await server.connect(new StdioServerTransport())
