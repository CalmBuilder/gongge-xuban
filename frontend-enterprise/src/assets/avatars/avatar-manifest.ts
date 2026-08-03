import avatarAfterSales from './gongge-avatar-after-sales.png';
import avatarCommerce from './gongge-avatar-commerce.png';
import avatarDefault from './gongge-avatar-default.png';
import avatarKnowledge from './gongge-avatar-knowledge.png';
import avatarOps from './gongge-avatar-ops.png';
import avatarOverall from './gongge-avatar-overall.png';
import avatarQuality from './gongge-avatar-quality.png';
import avatarService from './gongge-avatar-service.png';

export type EmployeeAvatarAssetKey =
  | 'default'
  | 'service'
  | 'after-sales'
  | 'knowledge'
  | 'commerce'
  | 'ops'
  | 'quality'
  | 'overall';

/** Central source of the approved Gongge role avatar assets. */
export const AVATAR_ASSETS: Record<EmployeeAvatarAssetKey, string> = {
  default: avatarDefault,
  service: avatarService,
  'after-sales': avatarAfterSales,
  knowledge: avatarKnowledge,
  commerce: avatarCommerce,
  ops: avatarOps,
  quality: avatarQuality,
  overall: avatarOverall,
};
