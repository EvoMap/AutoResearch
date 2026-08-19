import { describe, expect, test } from 'bun:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  bindCriticReport,
  hasIndependentIdentityPair,
  persistCriticReport,
  renderBlindReview,
  requireIndependentCritics,
} from './ar-critic-contract'

describe('blind review persistence contract', () => {
  test('binds an external critic report to the engine unit and cycle', () => {
    const report = bindCriticReport(
      '- verdict: needs_more_research\n- required_next_focus: scale',
      'external_critic_after_pilot_result_analysis',
      2,
    )

    expect(
      report.startsWith(
        '- unit: external_critic_after_pilot_result_analysis\n- cycle: 2\n',
      ),
    ).toBe(true)
    expect(report).toContain('- verdict: needs_more_research')
    expect(() =>
      bindCriticReport('- verdict: finish_ok', 'bad\nunit', 0),
    ).toThrow()
  })

  test('persists the bound report at the only engine-consumed path', async () => {
    const projectRoot = await mkdtemp(join(tmpdir(), 'ar-critic-contract-'))
    const output = join(projectRoot, 'critic.md')
    try {
      const report = await persistCriticReport(
        '- verdict: needs_more_research\n- required_next_focus: scale',
        'external_critic_after_pilot_result_analysis',
        0,
        projectRoot,
        output,
      )

      expect(await readFile(output, 'utf8')).toBe(report)
      expect(report).toStartWith(
        '- unit: external_critic_after_pilot_result_analysis\n- cycle: 0\n',
      )
      await expect(
        persistCriticReport(
          '- verdict: finish_ok',
          'external_critic_after_pilot_result_analysis',
          0,
          projectRoot,
          join(projectRoot, 'forged.md'),
        ),
      ).rejects.toThrow('critic output must be')
    } finally {
      await rm(projectRoot, { recursive: true, force: true })
    }
  })

  test('self-test requires a pair of different ready identities', () => {
    expect(hasIndependentIdentityPair(['model-a'], ['model-a'])).toBe(false)
    expect(
      hasIndependentIdentityPair(['model-a'], ['MODEL-A', 'model-b']),
    ).toBe(true)
    expect(
      hasIndependentIdentityPair(['model-a', 'model-b'], ['model-b']),
    ).toBe(true)
  })

  test('keeps the fields consumed by the workflow engine', () => {
    const report = renderBlindReview([
      {
        role: 'critic',
        model: 'alias-a',
        modelIdentity: 'model-a',
        status: 'ok',
        text: '- rating: 8\n- decision: accept\n- top_weaknesses: weak baseline',
      },
      {
        role: 'critic_secondary',
        model: 'alias-b',
        modelIdentity: 'model-b',
        status: 'ok',
        text: '- rating: 4\n- decision: reject\n- top_weaknesses: missing ablation',
      },
    ])

    expect(report).toStartWith(
      '- avg_rating: 6.0\n- n_reviews: 2\n- decision: accept\n',
    )
  })

  test('does not count two aliases of one model twice', () => {
    const report = renderBlindReview([
      {
        role: 'critic',
        model: 'alias-a',
        modelIdentity: 'same-model',
        status: 'ok',
        text: '- rating: 8\n- decision: accept',
      },
      {
        role: 'critic_secondary',
        model: 'alias-b',
        modelIdentity: 'same-model',
        status: 'ok',
        text: '- rating: 2\n- decision: reject',
      },
    ])

    expect(report).toStartWith(
      '- avg_rating: 8.0\n- n_reviews: 1\n- decision: unavailable\n',
    )
  })

  test('the execution gate rejects a duplicated critic identity', () => {
    expect(() =>
      requireIndependentCritics(
        [
          {
            role: 'critic',
            model: 'alias-a',
            modelIdentity: 'same-model',
            status: 'ok',
            text: 'review a',
          },
          {
            role: 'critic_secondary',
            model: 'alias-b',
            modelIdentity: 'same-model',
            status: 'ok',
            text: 'review b',
          },
        ],
        'blind review',
      ),
    ).toThrow('requires 2 independent models')
  })

  test('an unconfigured optional second critic does not disable the primary', () => {
    const results = [
      {
        role: 'critic',
        model: 'alias-a',
        modelIdentity: 'model-a',
        status: 'ok' as const,
        text: '- rating: 8\n- decision: accept',
      },
      {
        role: 'critic_secondary',
        model: '',
        modelIdentity: '',
        status: 'skipped' as const,
        text: '',
        error: 'no ready model configured',
      },
    ]

    expect(requireIndependentCritics(results, 'blind review')).toHaveLength(1)
    expect(renderBlindReview(results)).toStartWith(
      '- avg_rating: 8.0\n- n_reviews: 1\n- decision: accept\n',
    )
  })
})
