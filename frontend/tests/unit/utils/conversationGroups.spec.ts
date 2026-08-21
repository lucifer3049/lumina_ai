/**
 * 對話清單的過濾與日期分組。
 *
 * 分組的邊界（今天／昨天／前 7 天／前 30 天）以「當地時間的日界」為準，不是
 * 「now 減 24 小時」——凌晨 00:10 時，昨晚 23:50 的對話是「昨天」，不是「今天」。
 */
import { describe, expect, it } from 'vitest'

import type { ConversationOut } from '@/types/models'
import { filterConversations, groupConversations } from '@/utils/conversationGroups'

const NOW = new Date('2026-08-21T10:00:00')

function conversation(overrides: Partial<ConversationOut>): ConversationOut {
  return {
    id: crypto.randomUUID(),
    kb_ids: [],
    last_message_at: null,
    message_count: 0,
    pinned: false,
    prompt_key: 'default',
    status: 'active',
    title: '',
    ...overrides,
  }
}

describe('filterConversations', () => {
  it('matches by title substring, case-insensitive', () => {
    const items = [
      conversation({ title: 'Docker 反向代理' }),
      conversation({ title: 'DNS 介紹' }),
      conversation({ title: 'docker SSL 憑證' }),
    ]

    const hits = filterConversations(items, 'docker')

    expect(hits.map((item) => item.title)).toEqual(['Docker 反向代理', 'docker SSL 憑證'])
  })

  it('returns everything when the query is blank', () => {
    const items = [conversation({ title: 'A' }), conversation({ title: '' })]

    expect(filterConversations(items, '   ')).toHaveLength(2)
  })
})

describe('groupConversations', () => {
  it('groups by local-day boundaries, not rolling 24h windows', () => {
    const items = [
      conversation({ title: '今早', last_message_at: '2026-08-21T01:00:00' }),
      conversation({ title: '昨晚', last_message_at: '2026-08-20T23:50:00' }),
      conversation({ title: '三天前', last_message_at: '2026-08-18T12:00:00' }),
      conversation({ title: '兩週前', last_message_at: '2026-08-05T12:00:00' }),
      conversation({ title: '去年', last_message_at: '2025-08-21T12:00:00' }),
    ]

    const groups = groupConversations(items, NOW)

    expect(groups.map((group) => group.label)).toEqual(['今天', '昨天', '前 7 天', '前 30 天', '更早'])
    expect(groups[0]?.items.map((item) => item.title)).toEqual(['今早'])
    expect(groups[1]?.items.map((item) => item.title)).toEqual(['昨晚'])
  })

  it('treats a conversation without messages as today (it was just created)', () => {
    const groups = groupConversations([conversation({ last_message_at: null })], NOW)

    expect(groups.map((group) => group.label)).toEqual(['今天'])
  })

  it('puts pinned conversations in their own leading group regardless of age', () => {
    const items = [
      conversation({ title: '新的', last_message_at: '2026-08-21T09:00:00' }),
      conversation({ title: '釘住的舊對話', last_message_at: '2025-01-01T00:00:00', pinned: true }),
    ]

    const groups = groupConversations(items, NOW)

    expect(groups.map((group) => group.label)).toEqual(['已釘選', '今天'])
    expect(groups[0]?.items.map((item) => item.title)).toEqual(['釘住的舊對話'])
  })

  it('omits empty groups and keeps input order inside a group', () => {
    const items = [
      conversation({ title: '較新', last_message_at: '2026-08-21T09:00:00' }),
      conversation({ title: '較舊', last_message_at: '2026-08-21T08:00:00' }),
    ]

    const groups = groupConversations(items, NOW)

    expect(groups).toHaveLength(1)
    expect(groups[0]?.items.map((item) => item.title)).toEqual(['較新', '較舊'])
  })
})
