import { describe, expect, test } from 'bun:test'
import { roleCheckFromFailure } from './ar-role-client'

describe('role bridge failures', () => {
  test('preserves the structured self-test report', () => {
    const check = roleCheckFromFailure(
      'critic',
      '{"ok":false,"role":"critic","configured_models":["primary","fallback"],"ready_models":["primary"],"ready_model_identities":["model-a"],"config":"/repo/config/providers.example.json"}',
      '',
      1,
    )

    expect(check).toEqual({
      ok: false,
      role: 'critic',
      configured_models: ['primary', 'fallback'],
      ready_models: ['primary'],
      ready_model_identities: ['model-a'],
      config: '/repo/config/providers.example.json',
      error: 'role bridge exited 1',
    })
  })
})
