/**
 * @Time       : 2026/10/10
 * @Author     : zhanglp8181
 * @File       : final-f3-browser-fixture.ts
 * @CallChain  : F3 Playwright案例 → 测试级Chromium租约 → 案例结束关闭浏览器
 * @Description: 逐案例释放Chromium进程，减少长full20运行中的浏览器内存残留。
 */
import { test as playwrightTest, type Browser, type BrowserContext, type TestInfo } from '@playwright/test';

type F3BrowserFixtures = { caseBrowser: Browser; context: BrowserContext };

let openedCount = 0;
let closedCount = 0;
let activeCount = 0;
const MEMORY_FREEZE_DIAGNOSTIC = process.env.F3_DIAGNOSTIC_SELECTION === 'memory-dta-freeze';

function remainingMemoryFreezeActiveMs(): number {
  /** 浏览器启动和测试级context共用运行器活动截止，不从fixture开始重置预算。 */
  const raw = process.env.F3_MEMORY_FREEZE_ACTIVE_DEADLINE_MONOTONIC_NS;
  if (!raw || !/^\d+$/.test(raw)) throw new Error('F3_MEMORY_FREEZE_ABSOLUTE_DEADLINE_MISSING');
  const remainingNs = BigInt(raw) - process.hrtime.bigint();
  return remainingNs <= 0n ? 1 : Number((remainingNs + 999_999n) / 1_000_000n);
}

/** 计算一个运行器入口绝对截止的剩余毫秒，过期时返回最小Playwright拒绝时限。 */
export function remainingAbsoluteFixtureDeadlineMs(deadlineNsRaw: string | undefined,
                                                  nowNs = process.hrtime.bigint()): number {
  if (!deadlineNsRaw || !/^\d+$/.test(deadlineNsRaw)) {
    throw new Error('F3_MEMORY_FREEZE_ABSOLUTE_DEADLINE_MISSING');
  }
  const remainingNs = BigInt(deadlineNsRaw) - nowNs;
  return remainingNs <= 0n ? 1 : Number((remainingNs + 999_999n) / 1_000_000n);
}

/** 在发起真实浏览器/上下文创建之前核同一单调绝对截止，过期时零派发。 */
export async function runBeforeAbsoluteFixtureDeadline<T>(
  deadlineNsRaw: string | undefined,
  stage: 'CHROMIUM_LAUNCH' | 'BROWSER_CONTEXT_CREATE',
  create: () => Promise<T>,
  nowNs = process.hrtime.bigint(),
): Promise<T> {
  if (!deadlineNsRaw || !/^\d+$/.test(deadlineNsRaw)) {
    throw new Error('F3_MEMORY_FREEZE_ABSOLUTE_DEADLINE_MISSING');
  }
  if (BigInt(deadlineNsRaw) <= nowNs) {
    throw new Error(`F3_MEMORY_FREEZE_ACTIVE_DEADLINE_EXHAUSTED_BEFORE_${stage}`);
  }
  return create();
}

/** 将本次夹具关闭绑定到所有context/browser teardown共享的原始绝对截止。 */
export async function closeWithinAbsoluteFixtureDeadline(
  testInfo: Pick<TestInfo, 'setTimeout'>,
  deadlineNsRaw: string | undefined,
  close: () => Promise<void>,
  nowNs = process.hrtime.bigint(),
): Promise<void> {
  testInfo.setTimeout(remainingAbsoluteFixtureDeadlineMs(deadlineNsRaw, nowNs));
  await close();
}

/** 返回当前worker里测试级浏览器租约计数，供夹具生命周期回归核验。 */
export function browserFixtureLifecycle(): { opened: number; closed: number; active: number } {
  return { opened: openedCount, closed: closedCount, active: activeCount };
}

/** 浏览器由Playwright配置决定，每个测试结束立即退出对应Chromium进程。 */
export const test = playwrightTest.extend<F3BrowserFixtures>({
  caseBrowser: [async ({ playwright, browserName, launchOptions, headless, channel }, use, testInfo) => {
    if (MEMORY_FREEZE_DIAGNOSTIC) testInfo.setTimeout(remainingMemoryFreezeActiveMs());
    const createBrowser = (): Promise<Browser> =>
      playwright[browserName].launch({ ...launchOptions, headless, channel });
    const browser = MEMORY_FREEZE_DIAGNOSTIC
      ? await runBeforeAbsoluteFixtureDeadline(
        process.env.F3_MEMORY_FREEZE_ACTIVE_DEADLINE_MONOTONIC_NS,
        'CHROMIUM_LAUNCH', createBrowser,
      )
      : await createBrowser();
    openedCount += 1;
    activeCount += 1;
    try {
      await use(browser);
    } finally {
      try {
        if (MEMORY_FREEZE_DIAGNOSTIC) {
          await closeWithinAbsoluteFixtureDeadline(
            testInfo,
            process.env.F3_MEMORY_FREEZE_FIXTURE_DEADLINE_MONOTONIC_NS,
            () => browser.close({ reason: 'F3 browser case complete' }),
          );
        } else {
          await browser.close({ reason: 'F3 browser case complete' });
        }
      } finally {
        closedCount += 1;
        activeCount -= 1;
      }
    }
  }, { scope: 'test', timeout: MEMORY_FREEZE_DIAGNOSTIC ? 30_000 : 0, title: 'F3 case browser' }],
  context: [async ({
    caseBrowser,
    acceptDownloads,
    baseURL,
    bypassCSP,
    clientCertificates,
    colorScheme,
    contextOptions,
    deviceScaleFactor,
    extraHTTPHeaders,
    geolocation,
    hasTouch,
    httpCredentials,
    ignoreHTTPSErrors,
    isMobile,
    javaScriptEnabled,
    locale,
    offline,
    permissions,
    proxy,
    serviceWorkers,
    storageState,
    timezoneId,
    userAgent,
    viewport,
  }, use, testInfo) => {
    if (MEMORY_FREEZE_DIAGNOSTIC) testInfo.setTimeout(remainingMemoryFreezeActiveMs());
    const createContext = (): Promise<BrowserContext> => caseBrowser.newContext({
      ...contextOptions,
      acceptDownloads,
      baseURL,
      bypassCSP,
      clientCertificates,
      colorScheme,
      deviceScaleFactor,
      extraHTTPHeaders,
      geolocation,
      hasTouch,
      httpCredentials,
      ignoreHTTPSErrors,
      isMobile,
      javaScriptEnabled,
      locale,
      offline,
      permissions,
      proxy,
      serviceWorkers,
      storageState,
      timezoneId,
      userAgent,
      viewport,
    });
    const context = MEMORY_FREEZE_DIAGNOSTIC
      ? await runBeforeAbsoluteFixtureDeadline(
        process.env.F3_MEMORY_FREEZE_ACTIVE_DEADLINE_MONOTONIC_NS,
        'BROWSER_CONTEXT_CREATE', createContext,
      )
      : await createContext();
    try {
      await use(context);
    } finally {
      if (MEMORY_FREEZE_DIAGNOSTIC) {
        await closeWithinAbsoluteFixtureDeadline(
          testInfo,
          process.env.F3_MEMORY_FREEZE_FIXTURE_DEADLINE_MONOTONIC_NS,
          () => context.close({ reason: 'F3 browser case complete' }),
        );
      } else await context.close({ reason: 'F3 browser case complete' });
    }
  }, { scope: 'test', timeout: MEMORY_FREEZE_DIAGNOSTIC ? 30_000 : undefined, title: 'F3 case context' }],
});
