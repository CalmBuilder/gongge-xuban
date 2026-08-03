import type { PlazaResourceKind } from './plaza-resource-icons';

import knowledgeArtwork from './gongge-plaza-knowledge-v2.png';
import generalSkillsArtwork from './gongge-plaza-skill-v2.png';
import skillsArtwork from './gongge-plaza-sop-v2.png';
import toolsArtwork from './gongge-plaza-tool-v2.png';

/**
 * 3D glass artwork for each plaza resource kind. These share the rendered,
 * dimensional look of the 数字员工 avatars, so the 知识库 / 技能 / SOP / 工具
 * modules feel like part of the same family as the employee cards.
 */
export const PLAZA_RESOURCE_ARTWORK = {
  knowledge: knowledgeArtwork,
  'general-skills': generalSkillsArtwork,
  skills: skillsArtwork,
  tools: toolsArtwork,
} satisfies Record<PlazaResourceKind, string>;
