import engineering01 from './gongge-expert-engineering-01.webp';
import engineering02 from './gongge-expert-engineering-02.webp';
import engineering03 from './gongge-expert-engineering-03.webp';
import engineering04 from './gongge-expert-engineering-04.webp';
import engineering05 from './gongge-expert-engineering-05.webp';
import engineering06 from './gongge-expert-engineering-06.webp';
import testing01 from './gongge-expert-testing-01.webp';
import testing02 from './gongge-expert-testing-02.webp';
import professionalServices01 from './gongge-expert-professional-services-01.webp';
import professionalServices02 from './gongge-expert-professional-services-02.webp';
import professionalServices03 from './gongge-expert-professional-services-03.webp';
import professionalServices04 from './gongge-expert-professional-services-04.webp';
import professionalServices05 from './gongge-expert-professional-services-05.webp';
import professionalServices06 from './gongge-expert-professional-services-06.webp';
import support01 from './gongge-expert-support-01.webp';
import support02 from './gongge-expert-support-02.webp';
import marketing01 from './gongge-expert-marketing-01.webp';
import marketing02 from './gongge-expert-marketing-02.webp';
import marketing03 from './gongge-expert-marketing-03.webp';
import marketing04 from './gongge-expert-marketing-04.webp';
import marketing05 from './gongge-expert-marketing-05.webp';
import paidMedia01 from './gongge-expert-paid-media-01.webp';
import paidMedia02 from './gongge-expert-paid-media-02.webp';
import sales01 from './gongge-expert-sales-01.webp';
import sales02 from './gongge-expert-sales-02.webp';
import gameDevelopment01 from './gongge-expert-game-development-01.webp';
import gameDevelopment02 from './gongge-expert-game-development-02.webp';
import gameDevelopment03 from './gongge-expert-game-development-03.webp';
import design01 from './gongge-expert-design-01.webp';
import design02 from './gongge-expert-design-02.webp';
import spatialComputing01 from './gongge-expert-spatial-computing-01.webp';
import spatialComputing02 from './gongge-expert-spatial-computing-02.webp';
import gis01 from './gongge-expert-gis-01.webp';
import gis02 from './gongge-expert-gis-02.webp';
import gis03 from './gongge-expert-gis-03.webp';
import security01 from './gongge-expert-security-01.webp';
import security02 from './gongge-expert-security-02.webp';
import security03 from './gongge-expert-security-03.webp';
import academic01 from './gongge-expert-academic-01.webp';
import academic02 from './gongge-expert-academic-02.webp';
import projectManagement01 from './gongge-expert-project-management-01.webp';
import projectManagement02 from './gongge-expert-project-management-02.webp';
import finance01 from './gongge-expert-finance-01.webp';
import finance02 from './gongge-expert-finance-02.webp';
import product01 from './gongge-expert-product-01.webp';
import product02 from './gongge-expert-product-02.webp';
import healthcare01 from './gongge-expert-healthcare-01.webp';
import healthcare02 from './gongge-expert-healthcare-02.webp';

export const EXPERT_AVATAR_POOLS = {
  专业服务: [professionalServices01, professionalServices02, professionalServices03, professionalServices04, professionalServices05, professionalServices06],
  工程研发: [engineering01, engineering02, engineering03, engineering04, engineering05, engineering06],
  市场营销: [marketing01, marketing02, marketing03, marketing04, marketing05],
  游戏开发: [gameDevelopment01, gameDevelopment02, gameDevelopment03],
  地理信息: [gis01, gis02, gis03],
  安全: [security01, security02, security03],
  设计创意: [design01, design02],
  销售: [sales01, sales02],
  测试质量: [testing01, testing02],
  付费媒体: [paidMedia01, paidMedia02],
  项目管理: [projectManagement01, projectManagement02],
  学术研究: [academic01, academic02],
  空间计算: [spatialComputing01, spatialComputing02],
  客户支持: [support01, support02],
  财务金融: [finance01, finance02],
  产品管理: [product01, product02],
  医疗健康: [healthcare01, healthcare02],
} as const;

export const ALL_EXPERT_AVATARS: readonly string[] = Object.values(EXPERT_AVATAR_POOLS).flat();

function stableAvatarIndex(value: string, poolLength: number): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % poolLength;
}

/** Resolve a category-aware expert avatar without persisting derived UI state. */
export function expertAvatarImage(category: string, stableKey: string): string {
  const categoryPool = EXPERT_AVATAR_POOLS[category as keyof typeof EXPERT_AVATAR_POOLS];
  const pool: readonly string[] = categoryPool || ALL_EXPERT_AVATARS;
  return pool[stableAvatarIndex(stableKey || category || 'expert', pool.length)];
}
