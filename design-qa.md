# 共格·序伴 reference UI QA

Source:
- Figma file `03XlzJQ1dFYdlDWBR4Mlg4`, page node `0:1`
- Covered reference frames: `1:2892`, `1:765`, `1:1462`, `1:2165`, `1:68`, `1:6578`, `1:3713`, `1:3425`, `1:5883`, `1:7470`, `1:3975`, `1:4614`, `1:4286`, `1:5013`, `1:5409`

Implementation checkpoints:
- Chat gallery and selected-session states: `/private/tmp/gongge-xuban-reference-qa/01-chat-gallery-switch.png` through `/private/tmp/gongge-xuban-reference-qa/15-chat-stopped-or-idle.png`
- Figma reference exports: `/private/tmp/gongge-xuban-figma-reference/01-figma-1-2892.png` through `/private/tmp/gongge-xuban-figma-reference/15-figma-1-5409.png`
- Visual comparison contact sheet: `/private/tmp/gongge-xuban-reference-visual-diff/overview-15-scenarios.png`
- Visual comparison report: `/private/tmp/gongge-xuban-reference-visual-diff/visual-diff-report.json`
- Enterprise employee roster: `/private/tmp/gongge-xuban-reference-qa/02-enterprise-agents-collapsed.png` (legacy filename; Figma node `1:765` is expanded), `/private/tmp/gongge-xuban-reference-qa/05-enterprise-agents-expanded.png`, `/private/tmp/gongge-xuban-reference-qa/09-enterprise-agents-collapsed-reference.png`
- Enterprise employee profile: `/private/tmp/gongge-xuban-reference-qa/06-enterprise-dashboard-expanded.png`, `/private/tmp/gongge-xuban-reference-qa/10-enterprise-dashboard-collapsed.png`
- Dark and responsive checks: `/private/tmp/gongge-xuban-reference-qa/20-dark-chat-input.png`, `/private/tmp/gongge-xuban-reference-qa/21-dark-enterprise-dashboard.png`, `/private/tmp/gongge-xuban-reference-qa/22-mobile-chat-input.png`, `/private/tmp/gongge-xuban-reference-qa/23-mobile-enterprise-agents.png`
- Machine-readable report: `/private/tmp/gongge-xuban-reference-qa/report.json`

Browser QA summary:
- 23 browser states checked: all 15 reference frames, 4 enterprise regression pages, 2 dark-mode pages, and 2 narrow-screen pages.
- Figma metadata was checked for all 15 nodes. Important interaction-state corrections: `1:2165` is the chat gallery employee-filter dropdown state, `1:4286` is an expanded-sidebar model-dropdown input state, and only `1:5883` is the collapsed enterprise employee roster; `1:765` and `1:68` are expanded-sidebar roster states.
- Formal API data path used: `/api/auth/login`, `/api/chat/agents`, `/api/chat/sessions`, `/api/enterprise/agents`, and page-owned enterprise/chat API calls.
- Layout checks passed: no horizontal overflow, no visible error toast, no pageerror, key 1440x900 chat dimensions matched the reference (`72/220` sidebar, `56` header, `570` empty state, `1078/960 x 100` composer).
- In-app Browser checks verified `/chat/gallery` at expanded `220px` sidebar with `所有员工` active, visible employee-filter dropdown, top-right `切换主题`/`刷新页面` only, `/enterprise/agents` summary labels, model-dropdown input state, and dark enterprise dashboard inversion.
- Chat polling was constrained to current/running sessions to prevent request storms and stacked `Failed to fetch` errors.
- Enterprise knowledge page now suppresses non-visible OKF version probes during automatic page load.
- Follow-up in-app browser checks matched the reference top-right actions (`sun`/`refresh`), collapsed chat bottom icon, and composer shallow border/shadow.
- Dark-mode checks verified enterprise dashboard/agents and chat surfaces all invert their main content areas, not only the sidebars.

Automated checks:
- `npm --prefix frontend-enterprise run build` -> passed
- `node /private/tmp/codex-playwright/reference-qa.mjs` -> 23 total, 0 failures

Final result: passed
