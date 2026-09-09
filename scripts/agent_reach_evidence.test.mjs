/**
 * @Time       : 2026/09/05
 * @Author     : zhanglp8181
 * @File       : agent_reach_evidence.test.mjs
 * @CallChain  : node --test → 专家/双用户共用验收器 → 来源、页面和落盘负例
 * @Description: 不访问网络或历史证据，验证真实回执、完成态、隔离与自然问题门禁。
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import * as evidenceHelpers from './agent_reach_evidence.mjs';
import {
  assertBrowserClean, assertCompletedStream, assertDeniedAccess, assertFinalDelivery,
  assertResearchEvidence, buildResearchPrompt, isExpectedDenial, normalizeSourceUrl,
  observeBrowser, readOperationEvidence, reportUrls, requirementsForCase, runRecordedCheck, sanitizeEvidence,
  structuredReadReceipts, trackCompletedStream, waitForTerminalReport,
} from './agent_reach_evidence.mjs';

const A = 'https://example.org/primary';
const B = 'https://example.org/related';
const C = 'https://example.org/candidate';
const instant = '2026-09-05T01:00:00Z';
const requirements = { minimumReadSources: 2, focusDimensions: ['版本', '错误提示'] };

/** 构造不同正文的真实来源回执，不把搜索摘要当原文。 */
function readOperation(id, url = A, content = '原文版本与错误提示：' + id) {
  return { operation_id: id, operation_name: 'intelligence.web_read', status: 'succeeded',
    request: { url }, result: { data: { url, content, provider: 'Jina Reader', retrieved_at: instant } }, error: {} };
}

/** 按真实 Trace 的 data 信封构造可追溯发现结果。 */
function searchOperation(urls = [A, B, C]) {
  return { operation_id: 'search', operation_name: 'intelligence.web_search', status: 'succeeded',
    request: { query: '公开版本与错误实践' }, error: {}, result: { data: {
      result_state: 'success_with_data', count: urls.length, results: urls.map((url) => ({ url, title: url })),
    } } };
}

/** 测试报告仅包含业务章节与来源引用，工具名称不能作为来源判定依据。 */
function finalResult(evidence = A + '\n' + B) {
  return { status: 'verified', verification: { passed: true }, result: { markdown:
    '## 结论摘要\n比较版本实践。\n## 关键事实\n来源记录了版本与错误提示。\n## 来源与证据\n'
    + evidence + '\n## 冲突/缺口\n推断：适合试验；未知：成本。\n## 后续建议\n先验证兼容性。',
  } };
}

/** 组合完整固定证据供单一负例替换某一边界。 */
function fixture() {
  return { operations: [searchOperation(), readOperation('read-a'), readOperation('read-b', B)],
    executionResult: finalResult(), requirements };
}

/** 采用当前 github_repo 的 repository.content 合同，仓库描述不充当 README 原文。 */
function githubFixture(repository = {}, extra = {}) {
  const args = fixture();
  const url = 'https://github.com/example/tool';
  args.operations[0] = searchOperation([url, B]);
  args.operations[1] = { operation_id: 'repo-read', operation_name: 'intelligence.github_repo',
    status: 'succeeded', request: { repository: 'example/tool' }, error: {}, result: { data: {
      provider: 'GitHub public REST API', retrieved_at: instant,
      repository: { url, description: '仓库简短描述', content: 'README 原文：安装与命令行错误实践。', ...repository },
      issues: [], ...extra,
    } } };
  args.executionResult = finalResult(url + '\n' + B);
  return args;
}

test('同一网站不同原文可过机械门禁，专业审核仍待确认', () => {
  const result = assertResearchEvidence(fixture());
  assert.equal(result.distinctReadSourceCount, 2);
  assert.equal(result.automation, 'passed');
  assert.equal(result.professionalReview.status, 'pending');
});

for (const evidence of [
  '建议 intelligence.web_read 补读原文 ' + A + ' ' + B,
  'intelligence.web_read 未读原文，仅摘要 ' + A + ' ' + B,
  'Jina Reader 读取成功 ' + A + ' ，替换旧锚点 ' + B,
  '搜索结果 ' + A + ' ' + B + ' ' + C + '，实际两次读取同一来源。',
]) {
  test('拒绝报告文字充为双来源：' + evidence.slice(0, 22), () => {
    const args = fixture();
    args.operations[2] = readOperation('read-b', A);
    args.executionResult = finalResult(evidence);
    assert.throws(() => assertResearchEvidence(args), /不同实际来源/);
  });
}

test('真实读取即便存在，报告仅建议补读也不能冒充已引用证据', () => {
  const args = fixture();
  args.executionResult = finalResult('建议补读 ' + A + '\n未读原文 ' + B);
  assert.throws(() => assertResearchEvidence(args), /未在最终报告引用/);
});

test('带中文序号的来源章节仍能核对真实读取 URL', () => {
  const args = fixture();
  args.executionResult.result.markdown = args.executionResult.result.markdown
    .replace('## 来源与证据', '## 三、来源与证据');
  assert.equal(assertResearchEvidence(args).automation, 'passed');
});

test('不同请求重定向到同一有效 URL 不算双来源', () => {
  const args = fixture();
  args.operations[2].result.data.final_url = A;
  assert.throws(() => assertResearchEvidence(args), /不同实际来源/);
});

test('Jina 响应头披露相同最终来源时不能用不同请求 URL 凑数', () => {
  const args = fixture();
  args.operations[1].result.data.content = 'Title: A\nURL Source: ' + A + '\nMarkdown Content:\n第一次取得的正文';
  args.operations[2].result.data.content = 'Title: B\nURL Source: ' + A + '\nMarkdown Content:\n第二次取得的正文';
  assert.throws(() => assertResearchEvidence(args), /不同实际来源/);
});

test('不同网址但正文相同的转载不算独立证据', () => {
  const args = fixture();
  args.operations[1].result.data.content = 'Title: A\nURL Source: ' + A + '\nMarkdown Content:\n相同原文';
  args.operations[2].result.data.content = 'Title: B\nURL Source: ' + B + '\nMarkdown Content:\n相同原文';
  assert.throws(() => assertResearchEvidence(args), /不同实际来源/);
});

test('来源规范化不合并大小写敏感路径和不同业务查询', () => {
  assert.notEqual(normalizeSourceUrl('https://example.org/ABC'), normalizeSourceUrl('https://example.org/abc'));
  assert.notEqual(normalizeSourceUrl(A + '?id=1'), normalizeSourceUrl(A + '?id=2'));
  assert.equal(normalizeSourceUrl(A + '?utm_source=feed#section'), A);
  assert.equal(normalizeSourceUrl('javascript:alert(1)'), null);
});

test('来源引用解析兼容 URL 后紧接中文全角说明', () => {
  assert.deepEqual([...reportUrls('原文：https://github.com/google-gemini/gemini-cli（Jina Reader 降级）')],
    ['https://github.com/google-gemini/gemini-cli']);
  assert.deepEqual([...reportUrls('URL：https://example.org/report｜发布时间：2026-09-09')],
    ['https://example.org/report']);
});

for (const content of ['', ' \n ', 'Warning: Target URL returned error 404: Not Found\nMarkdown Content:\nNot Found', 'Title: Example\nMarkdown Content:\n']) {
  test('拒绝空白或错误页充当正文：' + content.slice(0, 25), () => {
    const args = fixture();
    args.operations[1].result.data.content = content;
    assert.throws(() => assertResearchEvidence(args), /可读正文/);
  });
}

test('没有回执 URL 或读取时间不能依据请求 URL 自证成功', () => {
  const args = fixture();
  delete args.operations[1].result.data.url;
  assert.throws(() => assertResearchEvidence(args), /返回的来源 URL/);
  args.operations[1] = readOperation('read-a');
  delete args.operations[1].result.data.retrieved_at;
  assert.throws(() => assertResearchEvidence(args), /获取时间/);
});

test('空数组、无 URL 服务提示和 count 冲突不能成为有效发现', () => {
  const args = fixture();
  const data = args.operations[0].result.data;
  data.results = []; data.count = 0;
  assert.throws(() => assertResearchEvidence(args), /空发现/);
  data.results = [{ title: 'Service unavailable' }]; data.count = 1;
  assert.throws(() => assertResearchEvidence(args), /可追溯 URL/);
  data.results = [{ url: A }]; data.count = 4;
  assert.throws(() => assertResearchEvidence(args), /count/);
});

test('搜索候选不能代替发现后的实际下钻', () => {
  const args = fixture();
  args.operations[0] = searchOperation([C]);
  assert.throws(() => assertResearchEvidence(args), /下钻/);
});

test('合法空搜索可恢复但要披露；10条不等于0条', () => {
  const args = fixture();
  args.operations.unshift({ ...searchOperation([]), operation_id: 'empty-search',
    result: { data: { results: [], result_state: 'no_results', count: 0 } } });
  args.executionResult = finalResult(A + '\n' + B + '\nempty-search 返回10条。');
  assert.throws(() => assertResearchEvidence(args), /零结果/);
  args.executionResult = finalResult(A + '\n' + B + '\nempty-search 返回0条（no_results），之后换词得到来源。');
  assert.equal(assertResearchEvidence(args).limitations.length, 1);
});

test('别的 Operation 披露空结果不能替代当前 Operation 的错误披露', () => {
  const args = fixture();
  args.operations.unshift({ ...searchOperation([]), operation_id: 'empty-search',
    result: { data: { results: [], result_state: 'no_results', count: 0 } } });
  args.executionResult = finalResult(A + '\n' + B + '\nempty-search 返回10条。\n另一个任务 intelligence.web_search：no_results。');
  assert.throws(() => assertResearchEvidence(args), /零结果/);
});

test('保留已恢复的失败，报告必须给出真实错误码', () => {
  const args = fixture();
  args.operations.unshift({ ...readOperation('failed-read', C), status: 'failed', result: {}, error: { code: 'PROVIDER_TIMEOUT' } });
  assert.throws(() => assertResearchEvidence(args), /定位失败/);
  args.executionResult = finalResult(A + '\n' + B + '\nfailed-read：PROVIDER_TIMEOUT，改读其他来源。');
  assert.equal(assertResearchEvidence(args).automation, 'passed');
});

for (const [label, repository, extra] of [
  ['仅描述', { content: undefined }, {}],
  ['非契约 readme 字段', { content: undefined, readme: '并非当前适配器返回的 content' }, {}],
  ['非契约顶层 content', { content: undefined }, { content: '不能冒充仓库 README' }],
  ['对象而非正文', { content: { text: '对象不能 stringify 后充正文' } }, {}],
]) {
  test('GitHub 不接受替代正文：' + label, () => {
    assert.throws(() => assertResearchEvidence(githubFixture(repository, extra)), /可读正文/);
  });
}

for (const [label, patch] of [
  ['顶层 partial', (data) => { data.partial = true; }],
  ['README partial', (data) => { data.repository.partial = true; }],
  ['README 截断', (data) => { data.repository.content_truncated = true; }],
  ['Issue 正文截断', (data) => { data.issues = [{ url: data.repository.url + '/issues/1',
    body: '只保留一段复现步骤', body_truncated: true }]; }],
  ['子资源错误', (data) => { data.subresource_errors = [{ resource: 'github.readme',
    url: 'https://api.github.com/repos/example/tool/readme', code: 'VERTICAL_ABILITY_PROVIDER_TIMEOUT' }]; }],
]) {
  test('拒绝未披露的部分结果：' + label, () => {
    const args = githubFixture();
    patch(args.operations[1].result.data);
    assert.throws(() => assertResearchEvidence(args), /限制|受限|部分|截断|子资源/);
  });
}

test('顶层网页截断及模型二次投影截断不能静默通过', () => {
  for (const flag of ['truncated', 'content_truncated']) {
    const args = fixture();
    args.operations[1].result.data[flag] = true;
    assert.throws(() => assertResearchEvidence(args), /限制|受限|截断/);
  }
});

test('模型投影显式裁剪尾注即使没有相邻标志也必须纳入限制', () => {
  const args = fixture();
  args.operations[1].result.data.content += '\n[模型上下文已截断：原始字段共 18000 字符，'
    + '仅展示开头内容；不得视为全文。]';
  assert.throws(() => assertResearchEvidence(args), /限制|受限|截断/);
  args.executionResult.result.markdown += '\nread-a：模型上下文已截断，仅核验已展示的正文片段。';
  const result = assertResearchEvidence(args);
  assert.equal(result.evidenceCompleteness, 'partial');
  assert.ok(result.limitations[0].markers.some((item) => item.kind === 'truncated'));
});

test('只有模型裁剪尾注不能充当实际来源正文', () => {
  const args = fixture();
  args.operations[1].result.data.content = '[模型上下文已截断：原始字段共 18000 字符，'
    + '仅展示开头内容；不得视为全文。]';
  assert.throws(() => assertResearchEvidence(args), /可读正文/);
});

test('披露部分原文和实际子资源错误后机械检查可过，但证据范围必须仍是 partial', () => {
  const args = githubFixture({ content_truncated: true, partial: true }, {
    partial: true, subresource_errors: [{ resource: 'github.readme',
      code: 'VERTICAL_ABILITY_PROVIDER_TIMEOUT', url: 'https://api.github.com/repos/example/tool/readme' }],
  });
  args.executionResult.result.markdown += '\nrepo-read：partial=true，content_truncated=true；'
    + 'github.readme VERTICAL_ABILITY_PROVIDER_TIMEOUT。仅核验已有正文片段，未完整读取，剩余内容待补。';
  const result = assertResearchEvidence(args);
  assert.equal(result.automation, 'passed');
  assert.equal(result.evidenceCompleteness, 'partial');
  assert.equal(result.professionalReview.status, 'pending');
  assert.equal(result.limitations.length, 1);
  assert.ok(result.limitations[0].markers.some((item) => item.kind === 'truncated'));
  assert.ok(result.limitations[0].markers.some((item) => item.code === 'VERTICAL_ABILITY_PROVIDER_TIMEOUT'));
});

test('泛称有限制不能代替对应 Operation 子资源的实际错误码', () => {
  const args = githubFixture({}, { partial: true, subresource_errors: [
    { resource: 'github.readme', code: 'VERTICAL_ABILITY_PROVIDER_TIMEOUT' },
  ] });
  args.executionResult.result.markdown += '\nrepo-read：仅部分获取，其他内容未知。';
  assert.throws(() => assertResearchEvidence(args), /子资源.*错误码/);
});

test('其他 Operation 的截断披露不能替代当前原文的限制', () => {
  const args = fixture();
  args.operations[1].result.data.truncated = true;
  args.executionResult.result.markdown += '\nread-b：truncated=true，仅取得片段。';
  assert.throws(() => assertResearchEvidence(args), /限制|受限|截断/);
});

test('部分回执不能被同一来源的完整读取或否认截断表述洗成完整证据', () => {
  for (const statement of [
    'read-a：已完整读取并核验该原文。',
    'read-a：truncated=false，未截断。',
    'read-a：truncated=true，已完整读取并核验该原文。',
  ]) {
    const args = fixture();
    args.operations[1].result.data.truncated = true;
    args.executionResult.result.markdown += '\n' + statement;
    assert.throws(() => assertResearchEvidence(args), /截断|完整/);
  }
});

test('限制回执明确否定完整性时应被正确识别为诚实披露', () => {
  const args = fixture();
  args.operations[1].result.data.truncated = true;
  args.executionResult.result.markdown += '\nread-a：truncated=true，以上片段不能视为完整读取。';
  const result = assertResearchEvidence(args);
  assert.equal(result.evidenceCompleteness, 'partial');
});

test('未触发的限制标志不增加来源或披露门槛', () => {
  const args = githubFixture({ content_truncated: false, partial: false }, {
    partial: false, subresource_errors: [],
  });
  assert.equal(assertResearchEvidence(args).limitations.length, 0);
});

test('Issue 业务只返回标题不通过，有正文和引用才成立', () => {
  const args = fixture();
  const repoUrl = 'https://github.com/example/tool';
  const issueUrl = repoUrl + '/issues/7';
  args.requirements = { ...requirements, needsIssueEvidence: true };
  args.operations[0] = searchOperation([repoUrl, B]);
  args.operations[1] = { operation_id: 'repo', operation_name: 'intelligence.github_repo', status: 'succeeded',
    request: { repository: 'example/tool' }, error: {}, result: { data: { provider: 'GitHub', retrieved_at: instant,
      repository: { url: repoUrl, description: '公开命令行工具及开发体验。',
        content: 'README 原文：该 CLI 的安装、退出码约定及跨平台用法。' },
      issues: [{ url: issueUrl, title: '错误码异常' }],
    } } };
  args.executionResult = finalResult(repoUrl + '\n' + B);
  assert.throws(() => assertResearchEvidence(args), /Issue 证据/);
  args.operations[1].result.data.issues[0].body = '升级后退出码不兼容，包含复现条件。';
  assert.throws(() => assertResearchEvidence(args), /Issue 原文没有/);
  args.executionResult = finalResult(repoUrl + '\n' + B + '\n' + issueUrl);
  assert.equal(assertResearchEvidence(args).automation, 'passed');
});

test('单次读取业务不新增多站点门槛', () => {
  const args = fixture();
  args.operations.pop();
  args.requirements = requirementsForCase({ research_question: '请说明这个公开项目的安装方式。', focus_dimensions: ['安装方式'] });
  args.executionResult = finalResult(A);
  assert.equal(assertResearchEvidence(args).distinctReadSourceCount, 1);
});

test('结构化公开条目带 URL 和正文时计入来源，标题-only 与空条目不计入', () => {
  const args = fixture();
  const topicA = 'https://www.v2ex.com/t/1001';
  const topicB = 'https://www.v2ex.com/t/1002';
  const collection = { operation_id: 'v2ex', operation_name: 'intelligence.v2ex_topics',
    status: 'succeeded', request: { mode: 'hot' }, error: {}, result: { data: {
      provider: 'V2EX public API', retrieved_at: instant, result_state: 'success_with_data', count: 4,
      items: [
        { url: topicA, title: '主题 A', content: '真实主题正文 A' },
        { url: topicB, title: '主题 B', content: '真实主题正文 B' },
        { url: 'https://www.v2ex.com/t/1003', title: '只有标题' },
        { title: '没有 URL 的正文', content: '不能追溯的正文' },
      ],
    } } };
  args.operations.push(collection);
  args.executionResult = finalResult(A + '\n' + B + '\n' + topicA + '\n' + topicB);
  const receipts = structuredReadReceipts(collection);
  assert.equal(receipts.length, 2);
  assert.equal(assertResearchEvidence(args).distinctReadSourceCount, 4);
  assert.deepEqual(receipts.map((item) => item.url), [topicA, topicB]);
});

test('只有标题或空正文的结构化集合不能绕过实际来源门禁', () => {
  const args = fixture();
  args.operations = [searchOperation(), readOperation('read-a')];
  args.requirements = { ...requirements, minimumReadSources: 2 };
  args.operations.push({ operation_id: 'v2ex-empty-content', operation_name: 'intelligence.v2ex_topics',
    status: 'succeeded', request: { mode: 'hot' }, error: {}, result: { data: {
      provider: 'V2EX public API', retrieved_at: instant, result_state: 'success_with_data', count: 2,
      items: [{ url: 'https://www.v2ex.com/t/1001', title: '只有标题' },
        { url: 'https://www.v2ex.com/t/1002', content: '' }],
    } } });
  args.executionResult = finalResult(A);
  assert.equal(structuredReadReceipts(args.operations[2]).length, 0);
  assert.throws(() => assertResearchEvidence(args), /不同实际来源/);
});

test('Trace 与 Execution 按真实 ID、名称、状态对账', () => {
  const raw = readOperation('read-a');
  const execution = { id: 'execution-a', operations: [{ id: raw.operation_id,
    operation_name: raw.operation_name, status: raw.status, effect_kind: 'read' }] };
  const trace = { sop_runtime: [{ instance_id: execution.id, operations: [raw] }] };
  assert.equal(readOperationEvidence(execution, trace)[0].result.data.url, A);
  assert.throws(() => readOperationEvidence(execution, { sop_runtime: [] }), /唯一/);
  trace.sop_runtime[0].operations[0] = { ...raw, status: 'failed' };
  assert.throws(() => readOperationEvidence(execution, trace), /状态/);
  trace.sop_runtime[0].operations[0] = { ...raw };
  delete trace.sop_runtime[0].operations[0].result;
  assert.throws(() => readOperationEvidence(execution, trace), /request\/result/);
});

test('整个矩阵默认自然问题不含工具、网址、工具次数或固定答案', async () => {
  const matrix = JSON.parse(await readFile(new URL('./agent_reach_expert_depth_matrix.json', import.meta.url), 'utf8'));
  assert.equal(matrix.length, 34);
  assert.equal(new Set(matrix.map((item) => item.category)).size, 17);
  for (const expertCase of matrix) {
    const prompt = buildResearchPrompt(expertCase);
    assert.equal(prompt, expertCase.natural_question || expertCase.research_question);
    assert.doesNotMatch(prompt, /intelligence\.|https?:\/\/|严格执行|两次|两条|web_read/);
  }
});

test('诊断模式只补业务维度，不加入固定源或工具路线', () => {
  const expertCase = { research_question: '比较公开项目。', focus_dimensions: ['版本', '错误提示'] };
  assert.equal(buildResearchPrompt(expertCase, 'diagnostic'), '比较公开项目。\n请重点关注：版本、错误提示。');
  assert.throws(() => buildResearchPrompt(expertCase, 'scripted'));
  assert.throws(() => buildResearchPrompt({ research_question: '调用 intelligence.web_read 读取 https://example.org' }));
});

test('SSE 必须有终帧且没有错误/取消帧，不能以 HTTP 200 充数', () => {
  assert.equal(assertCompletedStream('event: stream_delta\r\ndata: {}\r\n\r\nevent: complete\r\ndata: {}\r\n\r\n').complete, true);
  assert.throws(() => assertCompletedStream('event: stream_delta\ndata: {}\n\n'), /完成帧/);
  assert.throws(() => assertCompletedStream('event: error\ndata: {}\n\nevent: complete\ndata: {}\n\n'), /错误/);
});

test('SSE 探针不保存流正文，传输断开不能通过', async () => {
  const state = { complete: false };
  await trackCompletedStream({ status: () => 200, finished: async () => null,
    text: async () => 'event: complete\ndata: {"content":"测试正文"}\n\n' }, state);
  assert.deepEqual(state, { complete: true, eventTypes: ['complete'] });
  const failed = { complete: false };
  await trackCompletedStream({ status: () => 200, finished: async () => new Error('断开') }, failed);
  assert.equal(failed.complete, false);
  assert.ok(failed.error);
});

test('长 SSE 优先读取浏览器 Fetch 复制流，不依赖 DevTools 响应缓存', async () => {
  const state = { complete: false };
  const response = {
    status: () => { throw new Error('不应读取 DevTools 响应状态'); },
    finished: async () => null,
    text: async () => { throw new Error('不应读取 DevTools 响应正文'); },
  };
  const page = { evaluate: async () => ({ status: 200, text: 'event: complete\ndata: {}\n\n' }) };
  await trackCompletedStream(response, state, page);
  assert.deepEqual(state, { complete: true, eventTypes: ['complete'] });
});

test('结果 API 提前成功不能越过 SSE、持久消息或 DOM 门禁', () => {
  const executionResult = finalResult();
  const markdown = executionResult.result.markdown;
  const state = { streamComplete: true, assistants: [{ id: 'assistant', content: markdown }],
    executionResult, renderedText: markdown.replace(/^## /gm, ''), running: false };
  assert.equal(assertFinalDelivery(state).assistantId, 'assistant');
  for (const patch of [{ streamComplete: false }, { assistants: [] }, { running: true }, { renderedText: '只显示开头' }]) {
    assert.throws(() => assertFinalDelivery({ ...state, ...patch }));
  }
});

test('有序列表在 DOM 不含列表标号时仍能准确对账完整正文', () => {
  const executionResult = finalResult('1. 来源 ' + A + '\n2. 来源 ' + B);
  const markdown = executionResult.result.markdown;
  assert.doesNotThrow(() => assertFinalDelivery({ streamComplete: true,
    assistants: [{ id: 'assistant', content: markdown }], executionResult,
    renderedText: markdown.replace(/^## /gm, '').replace(/^\d+\. /gm, ''), running: false }));
});

test('Markdown 强调标题中的编号在 DOM 中省略时仍能准确对账完整正文', () => {
  const executionResult = finalResult('**1. 来源** ' + A + '\n**2. 证据** ' + B);
  const markdown = executionResult.result.markdown;
  const renderedText = markdown.replace(/^## /gm, '').replace(/\*\*\d+\.\s+/g, '**');
  assert.doesNotThrow(() => assertFinalDelivery({ streamComplete: true,
    assistants: [{ id: 'assistant', content: markdown }], executionResult,
    renderedText, running: false }));
});

test('API 已成功但最终页面未完成时继续轮询', async () => {
  const executionResult = finalResult();
  const markdown = executionResult.result.markdown;
  let waits = 0;
  const page = {
    locator: () => ({ last: () => ({ innerText: async () => waits ? markdown : '仍在生成' }) }),
    getByRole: () => ({ isVisible: async () => !waits }),
    waitForTimeout: async () => { waits += 1; },
  };
  const readJson = async (_, path) => {
    if (path.includes('/messages')) return waits ? [{ id: 'assistant', role: 'assistant', content: markdown }] : [];
    if (path.includes('/events')) return [{ event_type: 'dynamic_task_delegated', data: { execution_id: 'execution' } }];
    if (path.includes('/result')) return executionResult;
    return { id: 'execution', status: 'succeeded', current_result_id: 'result' };
  };
  const evidence = await waitForTerminalReport({ page, readJson, sessionId: 'session', tenantId: 'tenant',
    streamState: { complete: true }, pollAttempts: 3 });
  assert.equal(waits, 1);
  assert.equal(evidence.delivery.assistantId, 'assistant');
});

test('隔离必须拒绝携带对方数据的403/404，仅安全错误体允许', () => {
  assert.doesNotThrow(() => assertDeniedAccess({ status: 403, body: { detail: 'FORBIDDEN' } }, ['other-session']));
  for (const body of [
    { detail: 'FORBIDDEN', result: { markdown: '对方报告' } }, { detail: 'other-session' },
    { error: { content: '正文' } }, { detail: A }, { detail: ['用户消息'] }, {}, null,
  ]) assert.throws(() => assertDeniedAccess({ status: 403, body }, ['other-session', A]));
  assert.throws(() => assertDeniedAccess({ status: 200, body: { detail: 'FORBIDDEN' } }));
});

test('拒绝请求豁免必须匹配阶段、URL、方法、状态与已验证错误体', () => {
  const denial = { url: 'https://app.example/api/executions/other?tenant_id=tenant', status: 403, validated: true };
  const entry = { ...denial, phase: 'isolation', method: 'GET' };
  assert.equal(isExpectedDenial(entry, [denial]), true);
  for (const patch of [{ url: 'https://app.example/api/chat/abilities' }, { phase: 'research' }, { method: 'POST' }, { status: 404 }]) {
    assert.equal(isExpectedDenial({ ...entry, ...patch }, [denial]), false);
  }
  assert.equal(isExpectedDenial(entry, [{ ...denial, validated: false }]), false);
  assert.throws(() => assertBrowserClean({ denials: [denial], consoleErrors: [], httpErrors: [{ ...entry, url: A }] }));
});

test('页面观测器不全局吞掉 abilities 的403或404', () => {
  const listeners = new Map();
  const state = observeBrowser({ on: (name, callback) => listeners.set(name, callback) });
  for (const status of [403, 404]) {
    listeners.get('console')({ type: () => 'error',
      text: () => 'Failed to load resource: the server responded with a status of ' + status + ' (Denied)',
      location: () => ({ url: 'https://app.example/api/chat/abilities' }) });
  }
  assert.equal(state.consoleErrors.length, 2);
  assert.throws(() => assertBrowserClean(state), /浏览器错误/);
});

test('断言全部完成后才写成功，专业审核不自动置为通过', async () => {
  const writes = [];
  let assertionsComplete = false;
  const report = { stage: 'depth', executionResult: finalResult(), acceptance: { professionalReview: { status: 'pending' } } };
  const saved = await runRecordedCheck({ path: 'not-written.json', report,
    run: async () => { assert.equal(writes.length, 0); assertionsComplete = true; },
    write: async (value) => { assert.equal(assertionsComplete, true); writes.push(value); } });
  assert.equal(writes.length, 1);
  assert.equal(saved.status, 'completed');
  assert.equal(saved.acceptance.professionalReview.status, 'pending');
  assert.ok(saved.executionResult.result.markdown);
});

test('任意断言失败写 failed、阶段和截图，不能遗留 completed', async () => {
  const writes = [];
  await assert.rejects(() => runRecordedCheck({ path: 'not-written.json', report: { stage: 'depth' },
    run: async () => { throw new Error('重复来源'); }, captureFailure: async () => ['failure.png'],
    write: async (value) => writes.push(value) }), /验收失败/);
  assert.equal(writes.length, 1);
  assert.deepEqual({ status: writes[0].status, stage: writes[0].failedStage, screenshots: writes[0].failureScreenshots },
    { status: 'failed', stage: 'depth', screenshots: ['failure.png'] });
});

test('失败截图未保存也必须保留 failed JSON', async () => {
  let saved;
  await assert.rejects(() => runRecordedCheck({ path: 'not-written.json', report: { stage: 'login' },
    run: async () => { throw new Error('未登录'); }, captureFailure: async () => { throw new Error('页面关闭'); },
    write: async (value) => { saved = value; } }));
  assert.equal(saved.status, 'failed');
  assert.equal(saved.screenshotError, '失败截图未能保存');
});

test('证据递归脱敏且保留公开正文', () => {
  const safe = sanitizeEvidence({ Authorization: 'hidden', nested: { api_key: 'hidden', cookie: 'hidden' },
    markdown: '公开正文 Bearer private-value https://example.org/a?api_key=hidden password=hidden local-fixture-password' },
  ['local-fixture-password']);
  assert.equal(safe.Authorization, '[REDACTED]');
  assert.equal(safe.nested.cookie, '[REDACTED]');
  assert.ok(safe.markdown.includes('公开正文'));
  assert.doesNotMatch(JSON.stringify(safe), /private-value|hidden|local-fixture-password/);
});

test('证据脱敏不把 URL 后的中文说明误编码进链接', () => {
  const safe = sanitizeEvidence({
    markdown: '来源：https://example.org/a?api_key=hidden）：模型上下文已截断：原始字段共 12000 字符。',
  });
  assert.equal(
    safe.markdown,
    '来源：https://example.org/a?api_key=[REDACTED]）：模型上下文已截断：原始字段共 12000 字符。',
  );
});

test('两个入口共用校验和唯一证据路径，不残留报告 URL 猜测函数', async () => {
  for (const name of ['expert_depth', 'two_user_isolation']) {
    const text = await readFile(new URL('./live_agent_reach_' + name + '_browser_regression.mjs', import.meta.url), 'utf8');
    for (const identifier of ['runRecordedCheck', 'readOperationEvidence', 'waitForTerminalReport', 'buildResearchPrompt', 'randomUUID']) {
      assert.ok(text.includes(identifier));
    }
    assert.doesNotMatch(text, /distinctUrlsFromToolEvidence|function distinctUrls|403 \(Forbidden\)|404 \(Not Found\)/);
  }
});

test('fullstack 六处固定版本断言使用当前能力包版本', async () => {
  const text = await readFile(new URL('../frontend-enterprise/e2e/vertical-ability.fullstack.e2e.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(text, /version: '1\.6\.0'/);
  assert.equal([...text.matchAll(/version: '1\.7\.4'/g)].length, 6);
});

/** 执行实际 README 入口，只替代浏览器 I/O 和落盘载体；共享校验器仍运行原实现。 */
async function runReadmeOffline(patch = {}) {
  const source = await readFile(new URL('./live_agent_reach_readme_browser_regression.mjs', import.meta.url), 'utf8');
  const writes = [];
  const logs = [];
  const captures = [];
  const listeners = new Map();
  const executionResult = patch.executionResult || finalResult(A);
  const markdown = executionResult.result.markdown;
  const operations = patch.operations || [{ ...readOperation('read-a'), effect_kind: 'read' }];
  const execution = { id: 'execution-readme', kind: 'dynamic_task', status: 'succeeded',
    current_result_id: 'result-readme', operations: operations.map((row) => ({
      id: row.operation_id, operation_name: row.operation_name, status: row.status, effect_kind: row.effect_kind,
    })) };
  let waits = 0;
  let sseCaptureInstalled = false;
  const request = { method: () => 'POST', url: () => 'https://app.example/api/chat/stream',
    postDataJSON: () => ({ ability_ids: patch.wrongAbility ? [] : ['web-intelligence'], execution_engine: 'dynamic_task' }) };
  const response = { status: () => 200, request: () => request, url: request.url,
    finished: async () => null, text: async () => patch.sse || 'event: complete\ndata: {}\n\n' };
  const page = {
    on: (name, callback) => listeners.set(name, callback),
    addInitScript: async () => { sseCaptureInstalled = true; },
    goto: async () => {},
    reload: async () => {},
    url: () => 'https://app.example/workspace/chat/session_readme',
    waitForURL: async () => {},
    waitForFunction: async () => {},
    waitForTimeout: async () => { waits += 1; },
    screenshot: async () => {},
    waitForResponse: async () => response,
    getByPlaceholder: () => ({ fill: async () => {} }),
    getByRole: (_role, { name } = {}) => ({
      first: () => page.getByRole(_role, { name }),
      fill: async () => {}, waitFor: async () => {}, getAttribute: async () => 'true',
      isVisible: async () => false,
      click: async () => {
        if (name !== '发送') return;
        listeners.get('request')?.(request);
        listeners.get('response')?.(response);
        if (patch.httpStatus) {
          const denied = { method: () => 'GET',
            url: () => patch.httpUrl || 'https://app.example/api/chat/abilities' };
          listeners.get('request')?.(denied);
          listeners.get('response')?.({ status: () => patch.httpStatus, request: () => denied, url: denied.url });
          listeners.get('console')?.({ type: () => 'error', location: () => ({ url: denied.url() }),
            text: () => 'Failed to load resource: the server responded with a status of ' + patch.httpStatus + ' (Forbidden)' });
        }
      },
    }),
    locator: () => {
      const locator = { filter: () => locator, first: () => locator, last: () => locator,
        waitFor: async () => {}, click: async () => {}, getByRole: page.getByRole,
        textContent: async () => '能力包：全网情报员', innerText: async () => patch.partialDom ? '只显示开头' : markdown };
      return locator;
    },
    evaluate: async (_fn, path) => {
      if (typeof path !== 'string' || !path.startsWith('/api/')) {
        if (sseCaptureInstalled && String(_fn).includes('__gonggeXubanSseCapture')) {
          return { status: 200, text: patch.sse || 'event: complete\ndata: {}\n\n' };
        }
        return { id: 'reader-id', username: 'reader', role: 'member', tenantId: 'tenant' };
      }
      if (path === '/api/enterprise/vertical-ability-settings') {
        const status = patch.memberDenialStatus || 403;
        const denied = { method: () => patch.memberDenialMethod || 'GET',
          url: () => 'https://app.example' + path };
        listeners.get('request')?.(denied);
        listeners.get('response')?.({ status: () => status, request: () => denied, url: denied.url });
        listeners.get('console')?.({ type: () => 'error', location: () => ({ url: denied.url() }),
          text: () => 'Failed to load resource: the server responded with a status of ' + status + ' (Forbidden)' });
        return { status, body: patch.memberDenialBody || { detail: 'Only administrator can manage platform credentials' } };
      }
      if (path.includes('/messages')) return patch.missingAssistant ? [] : [{ id: 'assistant', role: 'assistant', content: markdown }];
      if (path.includes('/events')) return [{ event_type: 'dynamic_task_delegated', data: { execution_id: execution.id } }];
      if (path.includes('/result')) return executionResult;
      if (path.includes('/traces/')) {
        const rows = structuredClone(operations);
        if (patch.missingReceipt && rows[0]) delete rows[0].result;
        return { sop_runtime: [{ instance_id: execution.id, operations: rows }] };
      }
      return execution;
    },
  };
  const helpers = { ...evidenceHelpers,
    captureReportScreenshots: async (_page, prefix, options) => {
      captures.push({ prefix, options });
      return [prefix + '.png'];
    },
    runRecordedCheck: (args) => runRecordedCheck({ ...args, write: async (value) => writes.push(value) }),
  };
  const globals = { ...helpers, assert, randomUUID: () => 'offline-readme-invocation',
    chromium: { launch: async () => {
      if (patch.launchFailure) throw new Error('browser setup failed secret=fixture-private');
      return { newContext: async () => ({ newPage: async () => page, close: async () => {} }), close: async () => {} };
    } },
    process: { env: { BROWSER_TEST_BASE_URL: 'https://app.example', BROWSER_TEST_TENANT_ID: 'tenant',
      BROWSER_TEST_USERNAME: 'reader', BROWSER_TEST_PASSWORD: 'fixture-private',
      BROWSER_TEST_SCENARIO: patch.scenario || 'jina', BROWSER_TEST_EVIDENCE_POLL_ATTEMPTS: '2',
      BROWSER_TEST_PROMPT_MODE: 'natural', BROWSER_TEST_PROMPT: patch.prompt } },
    console: { log: (text) => logs.push(text), error: (text) => logs.push(text) },
  };
  // 仅移除静态 import，随后执行整份真实入口；不用复制待测入口或替换其断言方法。
  const script = source.replace(/^import[\s\S]*?from\s+['"][^'"]+['"];\s*$/gm, '');
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  let error = null;
  try { await new AsyncFunction(...Object.keys(globals), script)(...Object.values(globals)); }
  catch (caught) { error = caught; }
  return { error, writes, logs, waits, captures };
}

for (const httpStatus of [403, 404]) {
  test('README 实际入口不得全局吞掉普通用户 abilities HTTP ' + httpStatus, async () => {
    const run = await runReadmeOffline({ httpStatus });
    assert.ok(run.error, '非预期 HTTP 错误不能被旧入口当成成功');
    assert.equal(run.writes.length, 1, '必须持久保存失败，不能只输出控制台');
    assert.equal(run.writes[0].status, 'failed');
    assert.equal(run.writes[0].failedStage, 'browser_errors');
    assert.ok(run.logs.every((line) => !line.includes('"completed"')));
  });
}

test('README 全断言前不得输出 completed，错误只保存脱敏 failed 及阶段截图', async () => {
  const run = await runReadmeOffline({ wrongAbility: true });
  assert.ok(run.error);
  assert.ok(run.logs.every((line) => !line.includes('"completed"')), '旧入口在请求契约断言之前宣传 completed');
  assert.equal(run.writes.length, 1);
  assert.equal(run.writes[0].status, 'failed');
  assert.ok(run.writes[0].failureScreenshots.length);
  assert.ok(run.captures.every((capture) => capture.options.mask.length === 1));
  assert.doesNotMatch(JSON.stringify(run.writes), /fixture-private/);
});

for (const patch of [{ partialDom: true }, { missingAssistant: true }, { sse: 'event: stream_delta\ndata: {}\n\n' }]) {
  test('README 实际入口必须等待 SSE、持久助手和 DOM 共同完成：' + Object.keys(patch)[0], async () => {
    const run = await runReadmeOffline(patch);
    assert.ok(run.error, '只有成功结果 API 不构成实际最终交付');
    assert.equal(run.writes[0]?.status, 'failed');
    assert.equal(run.writes[0]?.failedStage, 'final_delivery');
  });
}

test('README 必须取得 Trace 真实请求结果，不能只看 OperationSummary', async () => {
  const run = await runReadmeOffline({ missingReceipt: true });
  assert.ok(run.error);
  assert.equal(run.writes[0]?.failedStage, 'operation_evidence');
});

test('README 浏览器启动阶段失败也保存脱敏失败，不要求已有会话', async () => {
  const run = await runReadmeOffline({ launchFailure: true });
  assert.ok(run.error);
  assert.equal(run.writes[0]?.status, 'failed');
  assert.equal(run.writes[0]?.failedStage, 'browser_setup');
  assert.doesNotMatch(JSON.stringify(run.writes), /fixture-private/);
});

test('README 单渠道诊断可以完成，但旧提示或 natural 环境变量不能冒充专家深度通过', async () => {
  const run = await runReadmeOffline();
  assert.equal(run.error, null);
  assert.equal(run.writes.length, 1);
  const saved = run.writes[0];
  assert.equal(saved.status, 'completed');
  assert.equal(saved.acceptance.scope, 'readme_diagnostic');
  assert.equal(saved.acceptance.naturalExpertDepth, 'not_evaluated');
  assert.equal(saved.acceptance.professionalReview.status, 'pending');
  assert.equal(saved.operations[0].result.data.content, '原文版本与错误提示：read-a');
  assert.ok(saved.delivery.renderedText);
  assert.equal(saved.stream.complete, true);
});

test('README 普通用户只豁免本次管理配置 GET 的已验证 403，研究阶段同 URL 不能沿用', async () => {
  const clean = await runReadmeOffline();
  assert.equal(clean.error, null);
  assert.deepEqual(clean.writes[0].browserObservation.denials, [{
    url: 'https://app.example/api/enterprise/vertical-ability-settings', status: 403, validated: true,
    body: { detail: 'Only administrator can manage platform credentials' },
  }]);
  const repeated = await runReadmeOffline({ httpStatus: 403,
    httpUrl: 'https://app.example/api/enterprise/vertical-ability-settings' });
  assert.ok(repeated.error);
  assert.equal(repeated.writes[0].failedStage, 'browser_errors');
});

for (const patch of [
  { memberDenialBody: { detail: 'Forbidden', data: { owner: 'another-user' } } },
  { memberDenialStatus: 404 }, { memberDenialMethod: 'POST' },
]) {
  test('README 权限拒绝的错误体、状态或方法不匹配不能豁免：' + Object.keys(patch)[0], async () => {
    const run = await runReadmeOffline(patch);
    assert.ok(run.error);
    assert.equal(run.writes[0].status, 'failed');
    assert.equal(run.logs.length, 0);
  });
}

/** 按实际集合信封构造 README 诊断样本；搜索/Feed/主题列表只证明库存而非原文下钻。 */
function readmeFixture(scenario) {
  const data = (items, key) => ({ [key]: items, count: items.length,
    result_state: items.length ? 'success_with_data' : 'no_results',
    provider: 'fixture', retrieved_at: instant });
  const collection = (name, key) => ({
    operation_id: name.split('.').at(-1), operation_name: name, status: 'succeeded', effect_kind: 'read',
    request: { query: '公开诊断', url: A }, error: {}, result: { data: data([{ url: A, title: '公开条目' }], key) },
  });
  const repo = { ...githubFixture().operations[1], effect_kind: 'read' };
  const reads = {
    jina: [{ ...readOperation('read-a'), effect_kind: 'read' }],
    exa: [collection('intelligence.web_search', 'results')],
    rss: [collection('intelligence.rss_read', 'entries')],
    v2ex: [collection('intelligence.v2ex_topics', 'items')],
    github: [repo, collection('intelligence.github_search', 'results')],
    coverage: [],
  };
  const operations = scenario === 'full-readme'
    ? ['jina', 'exa', 'rss', 'v2ex', 'github'].flatMap((key) => reads[key]) : reads[scenario];
  return { scenario, operations, executionResult: finalResult(scenario === 'coverage'
    ? '本场景未实际调用工具，没有实际外部来源；目录盘点未验证渠道或专业能力。'
    : A + '\nhttps://github.com/example/tool') };
}

for (const scenario of ['full-readme', 'jina', 'exa', 'rss', 'v2ex', 'github', 'coverage']) {
  test('README 保留既有入口且仅产出诊断判定：' + scenario, async () => {
    const run = await runReadmeOffline(readmeFixture(scenario));
    assert.equal(run.error, null);
    const saved = run.writes[0];
    assert.equal(saved.status, 'completed');
    assert.equal(saved.acceptance.scope, 'readme_diagnostic');
    assert.equal(saved.acceptance.naturalExpertDepth, 'not_evaluated');
    assert.equal(saved.acceptance.professionalReview.status, 'pending');
    assert.equal(saved.acceptance.outcomes.length, readmeFixture(scenario).operations.length);
    if (['rss', 'v2ex', 'exa'].includes(scenario)) {
      assert.equal(saved.acceptance.outcomes[0].evidenceKind, 'collection_inventory');
      assert.equal(saved.acceptance.outcomes[0].receipt, undefined);
    }
  });
}

test('README 场景缺调用、写操作、未收敛或只有仓库描述均须失败', async () => {
  for (const change of [
    (args) => { args.operations = []; },
    (args) => { args.operations[0].effect_kind = 'external_write'; },
    (args) => { args.operations[0].status = 'running'; },
    (args) => { args.operations[0].result.data.repository.content = ''; },
  ]) {
    const args = readmeFixture('github');
    change(args);
    const run = await runReadmeOffline(args);
    assert.ok(run.error);
    assert.equal(run.writes[0].status, 'failed');
    assert.equal(run.writes[0].failedStage, 'readme_diagnostic');
  }
});

test('README 集合空结果必须状态/count一致并披露，不能标为渠道读取成功', async () => {
  const args = readmeFixture('exa');
  Object.assign(args.operations[0].result.data, { results: [], count: 0 });
  let run = await runReadmeOffline(args);
  assert.equal(run.writes[0].status, 'failed');
  args.operations[0].result.data.result_state = 'no_results';
  run = await runReadmeOffline(args);
  assert.equal(run.writes[0].status, 'failed', '合法空结果也必须在实际报告披露');
  args.executionResult.result.markdown += '\nweb_search：no_results，0条结果。';
  run = await runReadmeOffline(args);
  assert.equal(run.error, null);
  assert.equal(run.writes[0].acceptance.outcomes[0].outcome, 'no_results');
  assert.deepEqual(run.writes[0].acceptance.outcomes[0].sources, []);
});

test('README Provider 失败可保留为诊断结果，但报告必须披露对应错误并且不是渠道成功', async () => {
  const args = readmeFixture('jina');
  Object.assign(args.operations[0], { status: 'failed', result: {}, error: { code: 'FIXTURE_TIMEOUT' } });
  let run = await runReadmeOffline(args);
  assert.equal(run.writes[0].status, 'failed');
  args.executionResult.result.markdown += '\nread-a：FIXTURE_TIMEOUT，读取失败，未取得正文。';
  run = await runReadmeOffline(args);
  assert.equal(run.error, null);
  assert.equal(run.writes[0].acceptance.outcomes[0].outcome, 'failed');
  assert.equal(run.writes[0].acceptance.naturalExpertDepth, 'not_evaluated');
});

test('README 部分结果和模型裁剪尾注必须对应披露，不能只依赖 verified', async () => {
  const args = readmeFixture('jina');
  Object.assign(args.operations[0].result.data, { partial: true,
    subresource_errors: [{ code: 'FIXTURE_SUBRESOURCE_TIMEOUT' }] });
  args.operations[0].result.data.content += '\n[模型上下文已截断：原始字段共 18000 字符，仅展示开头内容；不得视为全文。]';
  let run = await runReadmeOffline(args);
  assert.equal(run.writes[0].status, 'failed');
  args.executionResult.result.markdown += '\nread-a：partial=true，正文被截断，仅核验片段；FIXTURE_SUBRESOURCE_TIMEOUT。';
  run = await runReadmeOffline(args);
  assert.equal(run.error, null);
  assert.equal(run.writes[0].acceptance.outcomes[0].outcome, 'partial');
});

test('README 覆盖盘点不能夹带真实外呼或在无调用时隐去未验证范围', async () => {
  const args = readmeFixture('coverage');
  args.operations = readmeFixture('jina').operations;
  let run = await runReadmeOffline(args);
  assert.equal(run.writes[0].status, 'failed');
  args.operations = [];
  args.executionResult = finalResult('已经得到全部平台原文。');
  run = await runReadmeOffline(args);
  assert.equal(run.writes[0].status, 'failed');
});

test('README 未知场景仍保存配置阶段失败，不能跳过失败持久化', async () => {
  const run = await runReadmeOffline({ scenario: 'natural-expert' });
  assert.ok(run.error);
  assert.equal(run.writes[0].status, 'failed');
  assert.equal(run.writes[0].failedStage, 'configuration');
  assert.equal(run.writes[0].acceptance.scope, 'readme_diagnostic');
});

test('README 自定义自然措辞仍是诊断，不能取消该场景的真实调用断言', async () => {
  const prompt = '请说明这个公开页面的主要信息。';
  const complete = await runReadmeOffline({ prompt });
  assert.equal(complete.error, null);
  assert.equal(complete.writes[0].prompt, prompt);
  assert.equal(complete.writes[0].promptMode, 'readme_diagnostic');
  assert.equal(complete.writes[0].acceptance.naturalExpertDepth, 'not_evaluated');
  const empty = await runReadmeOffline({ prompt, operations: [] });
  assert.ok(empty.error);
  assert.equal(empty.writes[0].failedStage, 'readme_diagnostic');
});

test('共享截图器在登录页与报告分屏均透传密码遮罩，旧入口默认参数仍兼容', async () => {
  for (const hasReport of [false, true]) {
    const shots = [];
    const mask = [{ fixture: 'password-locator' }];
    const report = {
      count: async () => hasReport ? 1 : 0,
      evaluate: async () => ({ top: 0, height: 400, step: 200 }),
    };
    const page = { locator: () => ({ last: () => report }),
      evaluate: async () => {}, screenshot: async (options) => shots.push(options) };
    // screenshot 仅记录参数；prefix 的父目录是既有当前目录，不创建图片或历史证据。
    const paths = await evidenceHelpers.captureReportScreenshots(page, './not-written-readme', { mask });
    assert.equal(paths.length, hasReport ? 2 : 1);
    assert.ok(shots.every((shot) => shot.mask === mask));
    shots.length = 0;
    await evidenceHelpers.captureReportScreenshots(page, './not-written-readme');
    assert.ok(shots.every((shot) => Array.isArray(shot.mask) && shot.mask.length === 0));
  }
});
