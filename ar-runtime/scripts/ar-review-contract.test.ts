import { describe, expect, test } from 'bun:test'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  attachReviewProvenance,
  persistReviewReport,
} from './ar-review-contract'

describe('review provenance', () => {
  test('injects the route identity into existing frontmatter', () => {
    const review = attachReviewProvenance(
      '---\nmodel: self-claimed\nmodel_identity: self/claimed\nblockers_count: 0\n---\n\n# Review\n',
      'azure-deepseek-v4-pro',
      'deepseek-v4-pro',
    )

    expect(review).toStartWith(
      '---\nmodel: azure-deepseek-v4-pro\nmodel_identity: deepseek-v4-pro\nblockers_count: 0\n---',
    )
    expect(review).not.toContain('self-claimed')
    expect(review.match(/^model:/gm)).toHaveLength(1)
    expect(review.match(/^model_identity:/gm)).toHaveLength(1)
  })

  test('wraps malformed markdown without losing the review text', () => {
    const review = attachReviewProvenance(
      'plain review',
      'route-a',
      'identity-a',
    )

    expect(review).toContain('model: route-a')
    expect(review).toContain('model_identity: identity-a')
    expect(review).toEndWith('plain review')
  })

  test('unwraps a whole markdown fence before attaching provenance', () => {
    const review = attachReviewProvenance(
      '```markdown\n---\nblockers_count: 0\nwarnings_count: 0\n---\n\n# Review\n```',
      'route-a',
      'identity-a',
      'review_iteration_c1',
      1,
    )

    expect(review).toStartWith(
      '---\nmodel: route-a\nmodel_identity: identity-a\nreviewer: gemini-mcp-tool\nunit: review_iteration_c1\ncycle: 1\nblockers_count: 0\n',
    )
    expect(review).not.toContain('```markdown')
    expect(review.match(/^---$/gm)).toHaveLength(2)
  })

  test('recovers a copied count placeholder from strict blocker findings', () => {
    const review = attachReviewProvenance(
      [
        '---',
        'blockers_count: <int>',
        'warnings_count: 0',
        '---',
        '',
        '# Review',
        '',
        '## Blockers (must fix before running)',
        '- [B1] <severity:high> src/main.py:1: real issue | impact: crash',
      ].join('\n'),
      'route-a',
      'identity-a',
      'code_review',
      0,
    )

    expect(review).toContain('\nblockers_count: 1\n')
    expect(review).not.toContain('blockers_count: <int>')
  })

  test('does not turn unstructured blocker prose into a clean review', () => {
    const review = attachReviewProvenance(
      [
        '---',
        'blockers_count: <int>',
        '---',
        '',
        '# Review',
        '',
        '## Blockers (must fix before running)',
        'There is a serious issue, but it is not in the required format.',
      ].join('\n'),
      'route-a',
      'identity-a',
      'code_review',
      0,
    )

    expect(review).toContain('\nblockers_count: <int>\n')
    expect(review).not.toContain('\nblockers_count: 0\n')
  })

  test('rejects a hard-constraint violation classified below blocker', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-review-constraint-'))
    const output = join(projectRoot, 'review.md')
    try {
      await writeFile(
        join(projectRoot, 'workflow_queue.json'),
        JSON.stringify({
          units: [
            {
              id: 'code_review',
              type: 'review',
              cycle: 0,
              status: 'running',
            },
          ],
        }),
      )
      await expect(
        persistReviewReport(
          [
            '---',
            'blockers_count: 0',
            'warnings_count: 1',
            '---',
            '',
            '# Review',
            '',
            '## Blockers (must fix before running)',
            'None',
            '',
            '## Warnings (should fix)',
            '- [W1] fixed seed changes between repetitions',
            '',
            '## Constraint Audit',
            '- [C1] fixed seed | status: violated | evidence: code/main.py:12 uses seed + repetition | blocker: none',
          ].join('\n'),
          'route-a',
          'identity-a',
          'code_review',
          0,
          projectRoot,
          output,
        ),
      ).rejects.toThrow('hard-constraint audit requires blocker findings')
      await expect(readFile(output, 'utf8')).rejects.toThrow()
    } finally {
      await rm(projectRoot, { recursive: true, force: true })
    }
  })

  test('rejects persistence outside the reviewer MCP process', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-review-contract-'))
    const output = join(projectRoot, 'review.md')
    try {
      await writeFile(
        join(projectRoot, 'workflow_queue.json'),
        JSON.stringify({
          units: [
            {
              id: 'code_review',
              type: 'review',
              cycle: 0,
              status: 'running',
            },
          ],
        }),
      )
      await expect(
        persistReviewReport(
          '---\nblockers_count: 1\n---\n\n# Review\n\n## Blockers\n- [B1] <severity:high> code.py:1: failure | impact: invalid\n\n## Constraint Audit\n- [C1] must pass | status: violated | evidence: code.py:1 | blocker: B1\n',
          'azure-deepseek-v4-pro',
          'deepseek-v4-pro',
          'code_review',
          0,
          projectRoot,
          output,
        ),
      ).rejects.toThrow('producer')
      await expect(readFile(output, 'utf8')).rejects.toThrow()
      await expect(
        persistReviewReport(
          '---\nblockers_count: 0\n---\n\n## Constraint Audit\n- [C1] must pass | status: satisfied | evidence: code.py:1 | blocker: none\n',
          'model',
          'identity',
          'code_review',
          0,
          projectRoot,
          join(projectRoot, 'forged.md'),
        ),
      ).rejects.toThrow('review output must be')

      await writeFile(
        join(projectRoot, 'workflow_queue.json'),
        JSON.stringify({
          units: [
            {
              id: 'code_review',
              type: 'review',
              cycle: 0,
              status: 'done',
            },
          ],
        }),
      )
      await expect(
        persistReviewReport(
          '---\nblockers_count: 0\n---\n\n## Constraint Audit\n- [C1] must pass | status: satisfied | evidence: code.py:1 | blocker: none\n',
          'late-model',
          'late-identity',
          'code_review',
          0,
          projectRoot,
          output,
        ),
      ).rejects.toThrow('拒绝迟到 producer 覆盖产物')
      await expect(readFile(output, 'utf8')).rejects.toThrow()
    } finally {
      await rm(projectRoot, { recursive: true, force: true })
    }
  })
})
