import { describe, expect, it } from 'vitest';

import {
  ALL_EXPERT_AVATARS,
  EXPERT_AVATAR_POOLS,
  expertAvatarImage,
} from './expert-avatar-manifest';

const EXPECTED_POOL_SIZES: Record<string, number> = {
  专业服务: 6,
  工程研发: 6,
  市场营销: 5,
  游戏开发: 3,
  地理信息: 3,
  安全: 3,
  设计创意: 2,
  销售: 2,
  测试质量: 2,
  付费媒体: 2,
  项目管理: 2,
  学术研究: 2,
  空间计算: 2,
  客户支持: 2,
  财务金融: 2,
  产品管理: 2,
  医疗健康: 2,
};

describe('expert avatar manifest', () => {
  it('contains the approved 17 category pools and 48 unique WebP assets', () => {
    expect(Object.fromEntries(
      Object.entries(EXPERT_AVATAR_POOLS).map(([category, assets]) => [category, assets.length]),
    )).toEqual(EXPECTED_POOL_SIZES);
    expect(ALL_EXPERT_AVATARS).toHaveLength(48);
    expect(new Set(ALL_EXPERT_AVATARS)).toHaveLength(48);
    for (const asset of ALL_EXPERT_AVATARS) {
      expect(asset).toContain('gongge-expert-');
      expect(asset).toContain('.webp');
    }
  });

  it('resolves the same expert to the same category-aware avatar', () => {
    const first = expertAvatarImage('工程研发', 'engineering/frontend-developer.md');
    expect(first).toBe(expertAvatarImage('工程研发', 'engineering/frontend-developer.md'));
    expect(EXPERT_AVATAR_POOLS.工程研发).toContain(first);
  });

  it('falls back to the complete pool for an unknown category', () => {
    expect(ALL_EXPERT_AVATARS).toContain(expertAvatarImage('unknown-category', 'future/expert.md'));
  });
});
