import { describe, expect, it } from 'vitest';

import * as brandStorage from './brand-storage';

describe('brand storage contract', () => {
  it('exports only the current product storage keys', () => {
    expect(Object.keys(brandStorage)).toEqual(['BRAND_STORAGE_KEYS']);
  });

  it('uses only the current product key namespace', () => {
    expect(brandStorage.BRAND_STORAGE_KEYS).toEqual({
      locale: 'gongge_locale',
      onboardingSeen: 'gongge_onboarding_guide_seen',
      quickStartSeen: 'gongge_quick_start_guide_seen',
      authSession: 'gongge_auth',
      agentScope: 'gongge_enterprise_agent_scope',
      sidebarExpanded: 'gongge_enterprise_sidebar_expanded',
      sessionFilterPrefix: 'gongge_session_filter',
    });
  });
});
