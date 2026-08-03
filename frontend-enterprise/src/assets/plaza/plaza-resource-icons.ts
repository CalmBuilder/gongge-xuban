export type PlazaResourceKind = 'knowledge' | 'general-skills' | 'skills' | 'tools';

/** Stable resource categories shared by the plaza views and their semantic marks. */
export const PLAZA_RESOURCE_KINDS = [
  'knowledge',
  'general-skills',
  'skills',
  'tools',
] as const satisfies readonly PlazaResourceKind[];
