/**
 * @Time       : 2026/10/04
 * @Author     : zhanglp8181
 * @File       : final-f3-browser-driver.vitest.config.ts
 * @CallChain  : 主审串行资源守卫 → 单文件 Node Vitest → 纯 UI 请求合同
 * @Description: 不装载应用、Playwright runner、浏览器、服务或数据库的准备检查。
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['e2e/test-final-f3-browser-driver.test.ts'],
    pool: 'forks',
    maxWorkers: 1,
    fileParallelism: false,
    retry: 0,
    clearMocks: true,
  },
});
