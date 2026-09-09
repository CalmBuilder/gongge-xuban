/**
 * @Time       : 2026/09/05
 * @Author     : zhanglp8181
 * @File       : agent_reach_evidence.mjs
 * @CallChain  : README诊断/专家/双用户浏览器脚本 → Trace Operation → 分层验收与脱敏证据
 * @Description: 共用来源、完成态、隔离和落盘校验；自动检查不替代专家业务内容复审。
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const SECRET_KEY = /password|passwd|secret|token|authorization|cookie|api[_-]?key|credential/i;
const SEARCH_TOOLS = new Set(['intelligence.web_search', 'intelligence.github_search']);
const READ_TOOLS = new Set(['intelligence.web_read', 'intelligence.github_repo']);
const STRUCTURED_READ_COLLECTIONS = new Map([
  ['intelligence.rss_read', 'entries'],
  ['intelligence.v2ex_topics', 'items'],
]);
const REQUIRED_SECTIONS = ['结论摘要', '关键事实', '来源与证据', '冲突/缺口', '后续建议'];
const MODEL_CLIP_NOTICE = /\[模型上下文已截断：原始字段共 \d+ 字符，仅展示开头内容；不得视为全文。\]/gu;

/** 规范化实际 URL，保留大小写敏感路径和业务查询，只移除锚点与明确跟踪参数。 */
export function normalizeSourceUrl(value) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    url.hash = '';
    for (const key of [...url.searchParams.keys()]) {
      if (/^utm_/i.test(key) || /^(fbclid|gclid)$/i.test(key)) url.searchParams.delete(key);
    }
    url.searchParams.sort();
    return url.toString().replace(/\/$/, '');
  } catch {
    return null;
  }
}

/** 仅把报告链接用于交叉核对；链接本身永远不能创建成功读取回执。 */
export function reportUrls(markdown) {
  return new Set([...String(markdown).matchAll(/https?:\/\/[^\s<>\]"'（）｜|，。；：！？、【】《》]+/g)]
    .map((match) => normalizeSourceUrl(match[0].replace(/[),.;:!?，。；：！？、】》）｜|]+$/u, '')))
    .filter(Boolean));
}

/** 最终报告只在证据章节核对已读来源，明确建议/未读/摘要的行不能充当原文引用。 */
function citedReadUrls(markdown) {
  // 结果契约允许报告使用“## 三、来源与证据”这类带序号标题；不能因为
  // 展示层的编号而把真实读取 URL 误判为正文外的普通链接。按 Markdown
  // 标题分块后只合并来源章节，避免把“关键事实”或“后续建议”中的链接算作引用。
  const sourceHeading = /^#{1,6}\s*(?:(?:[一二三四五六七八九十百千万]+|\d+)[、.)]\s*)?来源与证据(?:\s|$)/u;
  const sections = String(markdown)
    .split(/(?=^#{1,6}\s)/m)
    .filter((block) => sourceHeading.test(block.split(/\r?\n/, 1)[0] || ''))
    .join('\n');
  return reportUrls(sections.split(/\r?\n/)
    .filter((line) => {
      if (!/建议|待(?:读|查|核)|未(?:读|取得)|仅(?:摘要|搜索)|只(?:有)?(?:摘要|搜索)|旧锚点|替换旧/u.test(line)) {
        return true;
      }
      // 一行可能同时记录“已读的主来源”和“尚未读取的辅助来源”。整行丢弃
      // 会误删主来源 URL；只要行内有明确的读取回执语义，实际 Operation 对账
      // 仍会限制最终可被接受的 URL，不会把报告文字变成新的来源。
      return /读取回执|已读原文|原文读取|实际读取|读取成功|Jina Reader/u.test(line);
    }).join('\n'));
}

/** 使用现有 Trace 的请求/结果，并与执行卡中的 Operation ID、名称和状态逐项对账。 */
export function readOperationEvidence(execution, trace) {
  const matches = (trace?.sop_runtime || []).filter((item) => item.instance_id === execution.id);
  assert.equal(matches.length, 1, '未找到唯一的当前 Execution Trace，不能推测来源');
  const rows = matches[0].operations;
  assert.ok(Array.isArray(rows), 'Trace 未暴露 Operation 回执，需要补充 API 契约');
  assert.ok(Array.isArray(execution.operations), 'Execution 缺少 Operation 摘要');
  assert.equal(new Set(rows.map((row) => row.operation_id)).size, rows.length, 'Trace Operation ID 重复');
  assert.equal(rows.length, execution.operations.length, 'Execution 与 Trace 的 Operation 数量不一致');
  return execution.operations.map((summary) => {
    const row = rows.find((item) => item.operation_id === summary.id);
    assert.ok(row, '执行卡中的 Operation 缺少真实 Trace 回执');
    assert.equal(row.operation_name, summary.operation_name, 'Operation 名称不一致');
    assert.equal(row.status, summary.status, 'Operation 状态不一致');
    assert.ok(Object.hasOwn(row, 'request') && Object.hasOwn(row, 'result'),
      'Trace 未暴露实际 request/result，禁止使用报告 URL 代替');
    return { ...row, effect_kind: summary.effect_kind };
  });
}

/** 验收默认只发送业务问题；诊断模式仅补关注维度，不写工具、次数、网址或固定答案。 */
export function buildResearchPrompt(expertCase, mode = 'natural') {
  assert.ok(['natural', 'diagnostic'].includes(mode), 'BROWSER_TEST_PROMPT_MODE 仅支持 natural/diagnostic');
  const question = String(expertCase.natural_question || expertCase.research_question || '').trim();
  assert.ok(question, '专家矩阵缺少自然业务问题');
  assert.doesNotMatch(question, /intelligence\.|https?:\/\/|\bweb_(?:search|read)\b/i,
    '自然业务问题不能包含内部工具或固定来源 URL');
  return mode === 'natural' ? question : `${question}\n请重点关注：${expertCase.focus_dimensions.join('、')}。`;
}

/** 从当前矩阵的业务表述确定交叉核验要求；单对象读取不附加两个网站的门槛。 */
export function requirementsForCase(expertCase) {
  const question = expertCase.research_question || '';
  return {
    minimumReadSources: /比较|对比|核对|核查|交叉|多来源|关联|竞品|文献|反馈/u.test(question) ? 2 : 1,
    needsRelatedEvidence: /Issue|社区讨论|社区反馈|用户反馈|真实问题反馈|已知问题/iu.test(question),
    needsIssueEvidence: /Issue/iu.test(question),
    focusDimensions: [...(expertCase.focus_dimensions || [])],
  };
}

/** 仅接受字符串原文，去掉 Jina 包装与模型裁剪尾注，拒绝错误页和系统说明充正文。 */
function readableBody(value) {
  const content = typeof value === 'string' ? value.trim() : '';
  if (!content || /Warning:\s*Target URL returned error\s*[45]\d\d/i.test(content)) return '';
  const raw = content.includes('Markdown Content:') ? content.split('Markdown Content:').slice(1).join('Markdown Content:') : content;
  const body = raw.replace(MODEL_CLIP_NOTICE, '').trim();
  return /^(?:not found|forbidden|access denied|service unavailable|404|403)\s*[.!]?$/i.test(body) ? '' : body;
}

/** 对实际内容计算指纹，识别重复回执与同文转载；不能推断不同内容就一定相互独立。 */
function contentChecksum(content) {
  return createHash('sha256').update(content.replace(/\s+/g, ' ').trim()).digest('hex');
}

/** 从成功读取结果形成唯一实际来源；请求网址只用于核对，不独立计数。 */
function readReceipt(operation) {
  const data = operation.result?.data;
  assert.ok(data && typeof data === 'object', '成功读取缺少结构化结果');
  assert.ok(!operation.error?.code && !data.error?.code, '成功读取携带错误，不能算有效来源');
  const repository = data.repository;
  let body = '';
  let sourceUrl;
  let related = [];
  if (operation.operation_name === 'intelligence.web_read') {
    body = readableBody(data.content);
    const header = String(data.content || '').includes('Markdown Content:')
      ? String(data.content).split('Markdown Content:', 1)[0] : '';
    const jinaSource = header.match(/^URL Source:\s*(https?:\/\/\S+)\s*$/m)?.[1];
    sourceUrl = data.final_url || data.canonical_url || jinaSource || data.url;
  } else {
    assert.ok(repository && typeof repository === 'object', '仓库读取缺少仓库详情');
    // 当前 github_repo 合同只以 repository.content 表示实际读取的 README/页面正文。
    body = readableBody(repository.content);
    sourceUrl = repository.final_url || repository.canonical_url || repository.url;
    related = (data.issues || []).filter((item) => readableBody(item.body || item.content))
      .map((item) => ({ url: normalizeSourceUrl(item.url), body: readableBody(item.body || item.content) }))
      .filter((item) => item.url);
  }
  const url = normalizeSourceUrl(sourceUrl);
  assert.ok(url, '成功读取缺少实际返回的来源 URL');
  assert.ok(body, '成功读取没有可读正文/详情，不能用标题、输入身份或空白充数');
  assert.ok(typeof data.retrieved_at === 'string' && Number.isFinite(Date.parse(data.retrieved_at)),
    '成功读取缺少合法的获取时间');
  assert.ok(typeof data.provider === 'string' && data.provider.trim(), '成功读取缺少 Provider');
  const repositoryRequest = operation.request?.repository;
  const requestUrl = normalizeSourceUrl(operation.request?.url || operation.request?.repository_url
    || (typeof repositoryRequest === 'string' && !repositoryRequest.includes('://')
      ? `https://github.com/${repositoryRequest}` : repositoryRequest));
  return {
    operationId: operation.operation_id,
    tool: operation.operation_name,
    requestUrl,
    url,
    aliases: [...new Set([url, requestUrl, normalizeSourceUrl(data.url)].filter(Boolean))],
    contentChecksum: contentChecksum(body),
    contentLength: body.length,
    retrievedAt: data.retrieved_at,
    provider: data.provider,
    related,
  };
}

/** 从结构化公开读取集合中提取带正文的条目；目录、标题-only 和空条目不能充当原文。 */
export function structuredReadReceipts(operation) {
  const collectionKey = STRUCTURED_READ_COLLECTIONS.get(operation.operation_name);
  assert.ok(collectionKey, '该 Operation 不是受支持的结构化读取能力');
  const data = operation.result?.data;
  assert.ok(data && typeof data === 'object', '结构化读取缺少结构化结果');
  assert.ok(!operation.error?.code && !data.error?.code, '结构化读取携带错误，不能算有效来源');
  assert.ok(Array.isArray(data[collectionKey]), '结构化读取缺少实际条目集合');
  const items = data[collectionKey];
  if (data.result_state === 'no_results') {
    assert.equal(items.length, 0, 'no_results 与结构化条目集合冲突');
    assert.equal(data.count, 0, 'no_results 与 count 冲突');
    return [];
  }
  assert.equal(data.result_state, 'success_with_data', '结构化读取缺少明确的有数据状态');
  assert.equal(data.count, items.length, '结构化读取 count 与集合冲突');
  assert.ok(items.length > 0, '空结构化读取不能算研究成功');
  assert.ok(typeof data.retrieved_at === 'string' && Number.isFinite(Date.parse(data.retrieved_at)),
    '结构化读取缺少合法的获取时间');
  assert.ok(typeof data.provider === 'string' && data.provider.trim(), '结构化读取缺少 Provider');
  const requestUrl = normalizeSourceUrl(operation.request?.url);
  return items.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const body = ['content', 'summary', 'description', 'body', 'text']
      .map((key) => readableBody(item[key]))
      .find(Boolean) || '';
    const url = normalizeSourceUrl(item.url || item.link || item.href);
    if (!url || !body) return [];
    return [{
      operationId: operation.operation_id,
      tool: operation.operation_name,
      requestUrl,
      url,
      aliases: [...new Set([url, requestUrl].filter(Boolean))],
      contentChecksum: contentChecksum(body),
      contentLength: body.length,
      retrievedAt: data.retrieved_at,
      provider: data.provider,
      related: [],
    }];
  });
}

/** 将限制说明绑定到具体 Operation；同名多次调用需要 ID 或实际查询/目标来消歧。 */
function operationDisclosure(markdown, operation, operations) {
  const sameName = operations.filter((item) => item.operation_name === operation.operation_name).length;
  const identifiers = [operation.request?.url, operation.request?.query, operation.request?.repository]
    .filter((item) => typeof item === 'string' && item.length >= 4);
  return markdown.split(/\r?\n/).filter((line) => line.includes(operation.operation_id)
    || (line.includes(operation.operation_name)
      && (sameName === 1 || identifiers.some((value) => line.includes(value))))).join('\n');
}

/** 递归保留 Provider/模型投影的明确限制事实；false 标志和普通描述不生成限制。 */
function resultLimitMarkers(data) {
  const markers = [];
  const visit = (value, path) => {
    if (typeof value === 'string') {
      if ([...value.matchAll(MODEL_CLIP_NOTICE)].length) {
        markers.push({ kind: 'truncated', path, source: 'model_projection_notice' });
      }
      return;
    }
    if (Array.isArray(value)) {
      value.forEach((item, index) => visit(item, `${path}[${index}]`));
      return;
    }
    if (!value || typeof value !== 'object') return;
    for (const [key, child] of Object.entries(value)) {
      const field = path ? `${path}.${key}` : key;
      if (child === true && (key === 'partial' || key === 'truncated' || key.endsWith('_truncated'))) {
        markers.push({ kind: key === 'partial' ? 'partial' : 'truncated', path: field });
      }
      if (key === 'subresource_errors' && Array.isArray(child)) {
        child.forEach((error, index) => markers.push({
          kind: 'subresource_error', path: `${field}[${index}]`,
          resource: typeof error?.resource === 'string' ? error.resource : undefined,
          code: typeof error?.code === 'string' ? error.code : undefined,
          url: normalizeSourceUrl(error?.url),
          statusCode: error?.status_code,
        }));
      } else visit(child, field);
    }
  };
  visit(data, '');
  return markers;
}

/** 识别限制披露中未经否定的“完整读取”声明，避免误伤“不能视为完整读取”。 */
function hasUnnegatedCompleteClaim(text) {
  const claimPattern = /(?:完整|全部)(?:地)?(?:读取|获取|核验|覆盖)/gu;
  for (const match of String(text).matchAll(claimPattern)) {
    const start = match.index ?? 0;
    const prefix = String(text).slice(Math.max(0, start - 12), start);
    if (!/(?:不能|无法|未|不|并非)(?:\s*(?:应|能|可))?(?:\s*(?:视为|算作|认定为|证明为|声称|代表))?\s*$/u.test(prefix)) {
      return true;
    }
  }
  return false;
}

/** 限制必须对应当前 Operation；裁剪回执不能被同一来源的完整读取表述洗成全文。 */
function assertLimitDisclosure(disclosure, markers) {
  if (markers.some((item) => item.kind === 'partial')) {
    assert.match(disclosure, /\bpartial\s*[:=]\s*true\b|部分|不完整|未完整/u,
      '报告未披露对应 Operation 的部分结果');
    assert.doesNotMatch(disclosure, /\bpartial\s*[:=]\s*false\b/iu,
      '报告否认对应 Operation 的部分结果');
  }
  if (markers.some((item) => item.kind === 'truncated')) {
    assert.match(disclosure, /truncated\s*[:=]\s*true\b|截断|裁剪|仅.{0,12}(?:片段|开头)/iu,
      '报告未披露对应 Operation 的截断限制');
    assert.doesNotMatch(disclosure, /truncated\s*[:=]\s*false\b|未截断|没有截断/iu,
      '截断回执不能否认实际截断');
    assert.equal(hasUnnegatedCompleteClaim(disclosure), false,
      '截断回执不能宣称完整读取或覆盖');
  }
  for (const marker of markers.filter((item) => item.kind === 'subresource_error')) {
    if (marker.code) {
      assert.ok(disclosure.includes(marker.code), '报告未披露对应 Operation 子资源的实际错误码');
    } else {
      assert.match(disclosure, /子资源|部分|失败|不可用/u, '报告未披露对应 Operation 的子资源错误');
    }
  }
}

/** 对真实 Operation 做来源/正文/引用门禁，保留失败和空结果，不把机械检查冒充专业审阅。 */
export function assertResearchEvidence({ operations, executionResult, requirements }) {
  assert.equal(executionResult?.status, 'verified', 'Execution 结果尚未验证');
  assert.equal(executionResult?.verification?.passed, true, 'Execution 结果验证未通过');
  const markdown = String(executionResult?.result?.markdown || '');
  for (const section of REQUIRED_SECTIONS) assert.ok(markdown.includes(section), `报告缺少${section}`);
  const successful = operations.filter((item) => item.status === 'succeeded');
  const searches = successful.filter((item) => SEARCH_TOOLS.has(item.operation_name));
  const discoveryUrls = new Set();
  for (const operation of searches) {
    const data = operation.result?.data;
    assert.ok(Array.isArray(data?.results), '发现 Operation 缺少实际结果集合');
    if (data.result_state === 'no_results') {
      assert.equal(data.results.length, 0, 'no_results 与实际集合冲突');
      assert.equal(data.count, 0, 'no_results 与 count 冲突');
      continue;
    }
    assert.equal(data.result_state, 'success_with_data', '发现 Operation 缺少明确的有数据状态');
    assert.equal(data.count, data.results.length, '发现结果 count 与集合冲突');
    assert.ok(data.results.length > 0, '空发现结果不能算研究成功');
    for (const item of data.results) {
      const url = normalizeSourceUrl(item.url);
      assert.ok(url, '发现结果缺少可追溯 URL');
      discoveryUrls.add(url);
    }
  }
  assert.ok(discoveryUrls.size > 0, '没有非空的实际发现回执');
  const reads = successful.filter((item) => READ_TOOLS.has(item.operation_name)).map(readReceipt);
  const structuredReads = successful.filter((item) => STRUCTURED_READ_COLLECTIONS.has(item.operation_name))
    .flatMap(structuredReadReceipts);
  assert.ok(reads.some((item) => item.aliases.some((url) => discoveryUrls.has(url))) || structuredReads.length > 0,
    '没有从实际发现结果下钻读取正文或取得带正文的结构化公开来源');
  reads.push(...structuredReads);
  const unique = [];
  for (const receipt of reads) {
    if (!unique.some((item) => item.url === receipt.url || item.contentChecksum === receipt.contentChecksum)) {
      unique.push(receipt);
    }
  }
  assert.ok(unique.length >= requirements.minimumReadSources,
    '未达到业务要求的不同实际来源：重复 URL、重定向及同文转载不能充数');
  const citations = citedReadUrls(markdown);
  for (const receipt of unique) {
    assert.ok(receipt.aliases.some((url) => citations.has(url)), '实际读取来源未在最终报告引用');
  }
  if (requirements.needsIssueEvidence) {
    const issueReads = reads.filter((item) => /\/issues\/\d+(?:$|[/?])/i.test(item.url));
    const issueDetails = reads.flatMap((item) => item.related);
    assert.ok(issueReads.length + issueDetails.length > 0, '业务要求 Issue 证据，但只有仓库摘要/Issue 标题');
    assert.ok([...issueReads, ...issueDetails].some((item) => citations.has(item.url)),
      '已取得的 Issue 原文没有在报告证据章节引用');
  }
  const limitations = operations.map((item) => ({ item, markers: resultLimitMarkers(item.result?.data) }))
    .filter(({ item, markers }) => item.status !== 'succeeded'
      || item.result?.data?.result_state === 'no_results' || markers.length > 0);
  for (const { item, markers } of limitations) {
    const disclosure = operationDisclosure(markdown, item, operations);
    assert.ok(disclosure, '报告未定位失败、空结果或受限 Operation，必须披露对应限制');
    if (item.error?.code) assert.ok(disclosure.includes(item.error.code), '报告未披露对应 Operation 的实际错误码');
    if (item.result?.data?.result_state === 'no_results') {
      assert.match(disclosure, /no_results|无结果|空结果|(?<!\d)0\s*条/u, '报告未披露对应 Operation 的真实零结果');
    }
    assertLimitDisclosure(disclosure, markers);
  }
  return {
    discoveryUrls: [...discoveryUrls],
    readReceipts: reads,
    distinctReadSourceCount: unique.length,
    limitations: limitations.map(({ item, markers }) => ({ operationId: item.operation_id,
      status: item.status, error: item.error, markers })),
    // 无明确标记不证明全文可得；partial 的诚实披露可过机械检查，但仍需专业复审。
    evidenceCompleteness: limitations.some(({ markers }) => markers.length) ? 'partial' : 'not_flagged',
    automation: 'passed',
    professionalReview: {
      status: 'pending',
      focusDimensions: requirements.focusDimensions,
      needsRelatedEvidence: requirements.needsRelatedEvidence,
      checks: ['专业维度是否有实际证据支撑', '关联信息是否完整或明确披露不足', '事实/推断/未知是否分开',
        '不同来源是否为独立证据而非改写转载', '结论是否真正回答用户问题'],
    },
  };
}

/** README 保留工具定向诊断入口；仅核对已取得的回执，不给自然专家深度或平台全集盖通过章。 */
export function assertReadmeDiagnosticEvidence({ scenario, operations, executionResult }) {
  const scenarios = {
    jina: ['intelligence.web_read'],
    exa: ['intelligence.web_search'],
    rss: ['intelligence.rss_read'],
    v2ex: ['intelligence.v2ex_topics'],
    github: ['intelligence.github_repo', 'intelligence.github_search'],
    'full-readme': ['intelligence.web_read', 'intelligence.web_search', 'intelligence.rss_read',
      'intelligence.v2ex_topics', 'intelligence.github_repo', 'intelligence.github_search'],
    coverage: [],
  };
  assert.ok(Object.hasOwn(scenarios, scenario), '未知 README 诊断场景');
  assert.ok(Array.isArray(operations), 'README 诊断缺少实际 Operation 集合');
  assert.equal(executionResult?.status, 'verified', 'README 诊断报告尚未验证');
  assert.equal(executionResult?.verification?.passed, true, 'README 诊断结果验证未通过');
  const markdown = executionResult?.result?.markdown;
  assert.ok(typeof markdown === 'string' && markdown.trim(), 'README 诊断缺少最终报告');
  for (const section of REQUIRED_SECTIONS) {
    assert.ok(markdown.includes(section), 'README 诊断报告缺少' + section);
  }
  const required = scenarios[scenario];
  if (scenario === 'coverage') {
    assert.equal(operations.length, 0, '覆盖盘点场景不得外呼，也不能拿回执冒充无调用');
    assert.match(markdown, /未实际调用|没有实际外部来源|未调用|未发起外部|未验证/u,
      '覆盖盘点必须披露没有实际外部验证');
  }
  for (const name of required) {
    assert.ok(operations.some((item) => item.operation_name === name), 'README 场景未实际尝试：' + name);
  }
  const citations = reportUrls(markdown);
  const readCitations = citedReadUrls(markdown);
  const outcomes = [];
  for (const operation of operations) {
    assert.ok(required.includes(operation.operation_name), 'README 场景调用了范围外工具');
    assert.equal(operation.effect_kind, 'read', 'README 诊断只允许公共读取');
    assert.ok(operation.request && typeof operation.request === 'object', 'README 缺少真实调用参数');
    assert.ok(['succeeded', 'failed'].includes(operation.status), 'README Operation 尚未正常收敛');
    const disclosure = operationDisclosure(markdown, operation, operations);
    if (operation.status === 'failed') {
      assert.ok(operation.error?.code && disclosure.includes(operation.error.code),
        'README 未披露对应失败 Operation 的真实错误码');
      assert.match(disclosure, /失败|不可用|未取得|未获取|超时|拒绝|failed|unavailable/iu,
        'README 失败回执不能只列错误码却宣称读取成功');
      outcomes.push({ operationId: operation.operation_id, tool: operation.operation_name,
        outcome: 'failed', error: operation.error, sources: [] });
      continue;
    }
    const data = operation.result?.data;
    assert.ok(data && typeof data === 'object', 'README 成功回执缺少结构化数据');
    assert.ok(!operation.error?.code && !data.error?.code, 'README 成功回执携带错误');
    assert.ok(typeof data.provider === 'string' && data.provider.trim(), 'README 成功回执缺少 Provider');
    assert.ok(typeof data.retrieved_at === 'string' && Number.isFinite(Date.parse(data.retrieved_at)),
      'README 成功回执缺少获取时间');
    const markers = resultLimitMarkers(data);
    if (markers.length) {
      assert.ok(disclosure, 'README 未定位受限 Operation');
      assertLimitDisclosure(disclosure, markers);
    }
    if (READ_TOOLS.has(operation.operation_name)) {
      const receipt = readReceipt(operation);
      assert.ok(receipt.aliases.some((url) => readCitations.has(url)), 'README 实际正文来源未被引用');
      outcomes.push({ operationId: operation.operation_id, tool: operation.operation_name,
        outcome: markers.length ? 'partial' : 'success_with_data', evidenceKind: 'read_content',
        sources: [receipt.url], receipt, markers });
      continue;
    }
    const key = SEARCH_TOOLS.has(operation.operation_name) ? 'results'
      : operation.operation_name === 'intelligence.rss_read' ? 'entries' : 'items';
    const items = data[key];
    assert.ok(Array.isArray(items), 'README 集合回执缺少实际条目');
    assert.equal(data.count, items.length, 'README count 与实际集合冲突');
    assert.equal(data.result_state, items.length ? 'success_with_data' : 'no_results',
      'README 空数组不能冒充有数据');
    const sources = items.map((item) => normalizeSourceUrl(item?.url));
    assert.ok(sources.every(Boolean), 'README 集合条目缺少实际来源 URL');
    assert.ok(sources.every((url) => citations.has(url)), 'README 未披露实际集合来源 URL');
    if (!items.length) {
      assert.match(disclosure, /no_results|无结果|空结果|(?<!\d)0\s*条/u,
        'README 未披露对应 Operation 的合法零结果');
    }
    outcomes.push({ operationId: operation.operation_id, tool: operation.operation_name,
      outcome: items.length ? (markers.length ? 'partial' : 'success_with_data') : 'no_results',
      evidenceKind: 'collection_inventory', sources, count: items.length, markers });
  }
  return {
    scope: 'readme_diagnostic', automation: 'passed', naturalExpertDepth: 'not_evaluated',
    professionalReview: { status: 'pending' }, scenario, outcomes,
    limitation: '仅核对所选 README 场景的回执与页面交付；集合不是逐条正文下钻，未证明全部平台或自然专家深度。',
  };
}

/** 将 Markdown 的可见文字规范化，与真实 DOM 对账而不把隐藏的链接目标算正文。 */
export function visibleReportText(value) {
  return String(value).replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    // Markdown 中的粗体/标题编号是格式标记，浏览器的 innerText 通常只保留标题文字；
    // 同时兼容普通有序列表，避免把“**1. 标题**”误判为页面漏字。
    .replace(/^(\s*(?:#{1,6}\s*)?(?:[*_]{1,3})?)\d+[.)]\s+/gm, '$1')
    .replace(/```[^\n]*\n/g, '').replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&(?:amp|lt|gt|quot|apos);/g, '')
    .replace(/[^\p{L}\p{N}]/gu, '');
}

/** 必须等到 SSE 终结、最终消息落账和 DOM 完整渲染，不能只看 Execution 先到的结果。 */
export function assertFinalDelivery({ streamComplete, assistants, executionResult, renderedText, running }) {
  assert.equal(streamComplete, true, 'SSE 尚未完整结束');
  assert.equal(running, false, '页面仍显示任务运行/生成');
  const expected = visibleReportText(executionResult?.result?.markdown || '');
  assert.ok(expected, '缺少最终报告');
  const assistant = [...assistants].reverse().find((item) => visibleReportText(item.content) === expected);
  assert.ok(assistant, '最终报告尚未完整落入助手消息');
  assert.equal(visibleReportText(renderedText), expected, '用户实际看到的 DOM 与最终报告不一致');
  return { assistantId: assistant.id, assistantMarkdown: assistant.content, renderedText };
}

/** 从完整 SSE 响应中检查终止帧；不保存或输出流中的用户内容。 */
export function assertCompletedStream(text) {
  const events = [];
  for (const frame of String(text).replace(/\r\n/g, '\n').split('\n\n')) {
    const event = frame.match(/^event:\s*(\S+)/m)?.[1];
    if (event) events.push(event);
  }
  assert.ok(events.some((event) => ['complete', 'done'].includes(event)), 'SSE 缺少完成帧');
  assert.ok(!events.some((event) => ['error', 'error_occurred', 'stream_cancelled', 'stream_interrupted'].includes(event)),
    'SSE 包含错误或中断帧');
  return { complete: true, eventTypes: [...new Set(events)] };
}

/** 在浏览器 Fetch 层复制 SSE 流，避免长响应结束后 Playwright DevTools 缓存驱逐正文。 */
export async function installSseCapture(page) {
  await page.addInitScript(() => {
    const state = { active: null, nextId: 0 };
    window.__gonggeXubanSseCapture = state;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await nativeFetch(...args);
      const input = args[0];
      const requestUrl = typeof input === 'string' ? input : input?.url;
      let pathname;
      try {
        pathname = new URL(requestUrl, window.location.href).pathname;
      } catch {
        return response;
      }
      if (pathname !== '/api/chat/stream' || !response.body) return response;
      const [applicationBody, captureBody] = response.body.tee();
      const capture = {
        id: ++state.nextId,
        status: response.status,
        statusText: response.statusText,
        text: null,
        error: null,
        done: false,
      };
      state.active = capture;
      new Response(captureBody).text().then((text) => {
        capture.text = text;
        capture.done = true;
      }).catch((error) => {
        capture.error = String(error);
        capture.done = true;
      });
      return new Response(applicationBody, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    };
  });
}

/** 读取浏览器 Fetch 层复制的完整 SSE，不依赖长响应的 DevTools body 缓存。 */
async function readCapturedSse(page) {
  return page.evaluate(async () => {
    const state = window.__gonggeXubanSseCapture;
    if (!state) throw new Error('浏览器 SSE 捕获器未安装');
    while (!state.active || !state.active.done) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const capture = state.active;
    state.active = null;
    if (capture.error) throw new Error(capture.error);
    return { status: capture.status, text: capture.text };
  });
}

/** 观察实际 SSE 请求结束并检查终帧，失败仅记录脱敏状态交给主流程拒绝验收。 */
export function trackCompletedStream(response, state, page = null) {
  return (async () => {
    try {
      const failure = await response.finished();
      assert.equal(failure, null, '聊天 SSE 传输未正常结束');
      const captured = page ? await readCapturedSse(page) : {
        status: response.status(), text: await response.text(),
      };
      assert.equal(captured.status, 200, '聊天 SSE 请求失败');
      Object.assign(state, assertCompletedStream(captured.text));
    } catch (error) {
      state.error = '聊天 SSE 传输或终帧验证失败';
      state.errorDetail = error instanceof Error ? error.message : String(error);
    }
  })();
}

/** 轮询持久事实并等待真实页面完成，不允许仅有结果 API 就提前拍照和保存成功。 */
export async function waitForTerminalReport({ page, readJson, sessionId, tenantId, streamState,
  pollAttempts = 900, onProgress = () => {} }) {
  for (let attempt = 0; attempt < pollAttempts; attempt += 1) {
    assert.ok(!streamState.error, streamState.error);
    const [messages, events] = await Promise.all([
      readJson(page, `/api/chat/sessions/${sessionId}/messages?tenant_id=${tenantId}`),
      readJson(page, `/api/chat/sessions/${sessionId}/events?tenant_id=${tenantId}`),
    ]);
    const assistants = messages.filter((item) => item.role === 'assistant' && item.content?.trim());
    const delegated = events.find((item) => item.event_type === 'dynamic_task_delegated');
    if (delegated?.data?.execution_id) {
      const executionId = String(delegated.data.execution_id);
      const execution = await readJson(page, `/api/executions/${executionId}?tenant_id=${tenantId}`);
      onProgress({ executionId, execution, eventTypes: events.map((item) => item.event_type) });
      if (['succeeded', 'failed', 'cancelled', 'timed_out'].includes(execution.status)) {
        const executionResult = execution.current_result_id
          ? await readJson(page, `/api/executions/${executionId}/result?tenant_id=${tenantId}`) : null;
        onProgress({ executionResult });
        if (execution.status !== 'succeeded') return { execution, executionId, executionResult, assistants, events };
        const renderedText = await page.locator('.gg-type-markdown').last().innerText().catch(() => '');
        const running = await page.getByRole('button', { name: '停止生成', exact: true }).isVisible();
        const expected = visibleReportText(executionResult?.result?.markdown || '');
        if (streamState.complete && !running && expected
          && assistants.some((item) => visibleReportText(item.content) === expected)
          && visibleReportText(renderedText) === expected) {
          const delivery = assertFinalDelivery({ streamComplete: streamState.complete, assistants,
            executionResult, renderedText, running });
          return { execution, executionId, executionResult, assistants, events, delivery };
        }
      }
    }
    await page.waitForTimeout(1_000);
  }
  throw new Error('等待超时：Execution、SSE、持久助手正文或用户可见 DOM 尚未共同完成');
}

/** 在真实容器中分屏截图并透传密码等遮罩；完整 DOM 另存报告，不修改页面样式。 */
export async function captureReportScreenshots(page, prefix, { mask = [] } = {}) {
  await mkdir(dirname(prefix), { recursive: true });
  const report = page.locator('.gg-type-markdown').last();
  if (!await report.count()) {
    const path = `${prefix}.png`;
    await page.screenshot({ path, fullPage: true, mask });
    return [path];
  }
  const geometry = await report.evaluate((element) => {
    let container = element.parentElement;
    while (container && !(container.scrollHeight > container.clientHeight
      && /auto|scroll/.test(getComputedStyle(container).overflowY))) container = container.parentElement;
    container ||= document.scrollingElement;
    const top = element.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
    return { top: Math.max(0, top), height: element.getBoundingClientRect().height,
      step: Math.max(200, Math.min(container.clientHeight, innerHeight) * 0.8) };
  });
  const count = Math.max(1, Math.ceil(geometry.height / geometry.step));
  assert.ok(count <= 100, '报告超过分屏截图上限，必须显式补验而非静默截断');
  const paths = [];
  for (let index = 0; index < count; index += 1) {
    await report.evaluate((element, top) => {
      let container = element.parentElement;
      while (container && !(container.scrollHeight > container.clientHeight
        && /auto|scroll/.test(getComputedStyle(container).overflowY))) container = container.parentElement;
      (container || document.scrollingElement).scrollTop = top;
    }, geometry.top + index * geometry.step);
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const path = `${prefix}-${index + 1}.png`;
    await page.screenshot({ path, mask });
    paths.push(path);
  }
  return paths;
}

/** 403/404 必须同时没有对方正文/来源等敏感字段，不能只断言状态码。 */
export function assertDeniedAccess(access, forbiddenValues = []) {
  assert.ok([403, 404].includes(access.status), '跨用户读取没有被拒绝');
  assert.ok(access.body && typeof access.body === 'object' && !Array.isArray(access.body), '隔离错误体不是可审计对象');
  const allowedKeys = new Set(['detail', 'code', 'message', 'error']);
  const walk = (value) => {
    if (typeof value === 'string') return;
    assert.ok(value && typeof value === 'object' && !Array.isArray(value), '隔离错误体包含非错误数据');
    const entries = Object.entries(value);
    assert.ok(entries.length > 0, '隔离错误体为空');
    for (const [key, child] of entries) {
      assert.ok(allowedKeys.has(key), '隔离错误体暴露非错误字段');
      walk(child);
    }
  };
  walk(access.body);
  const serialized = JSON.stringify(access.body);
  for (const value of forbiddenValues.filter((item) => typeof item === 'string' && item.length > 3)) {
    assert.ok(!serialized.includes(value), '隔离错误体包含对方会话/执行/报告/来源数据');
  }
}

/** 只豁免在隔离阶段发起、精确 URL/GET/状态匹配且错误体已验证的拒绝请求。 */
export function isExpectedDenial(entry, denials) {
  return entry.phase === 'isolation' && denials.some((item) => item.validated
    && item.url === entry.url && entry.method === 'GET' && item.status === entry.status);
}

/** 浏览器异常先全部留存，断言时才减去已逐请求验证的隔离拒绝。 */
export function observeBrowser(page) {
  const state = { phase: 'research', consoleErrors: [], httpErrors: [], denials: [], streamBodies: [] };
  const requestPhases = new WeakMap();
  page.on('request', (request) => {
    requestPhases.set(request, state.phase);
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/chat/stream') {
      try {
        const body = request.postDataJSON();
        state.streamBodies.push({ ability_ids: body?.ability_ids, execution_engine: body?.execution_engine });
      } catch {
        state.streamBodies.push({ malformed: true });
      }
    }
  });
  page.on('pageerror', (error) => state.consoleErrors.push({ text: error.message, phase: state.phase }));
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    const match = text.match(/^Failed to load resource: the server responded with a status of (403|404)\b/);
    state.consoleErrors.push({ text, url: message.location().url, phase: state.phase,
      method: match ? 'GET' : null, status: match ? Number(match[1]) : null });
  });
  page.on('response', (response) => {
    if (response.status() >= 400) state.httpErrors.push({ url: response.url(), status: response.status(),
      method: response.request().method(), phase: requestPhases.get(response.request()) });
  });
  page.on('requestfailed', (request) => state.consoleErrors.push({
    text: `requestfailed: ${new URL(request.url()).pathname}`, phase: requestPhases.get(request),
  }));
  return state;
}

/** 非预期 HTTP 错误和 JS 错误都拒绝验收，不做全局 403/404 忽略。 */
export function assertBrowserClean(state) {
  assert.equal(state.consoleErrors.filter((entry) => !isExpectedDenial(entry, state.denials)).length, 0,
    '存在非预期浏览器错误，详见脱敏证据');
  assert.equal(state.httpErrors.filter((entry) => !isExpectedDenial(entry, state.denials)).length, 0,
    '存在非预期 HTTP 错误，详见脱敏证据');
}

/** 写盘前递归去除凭据字段、认证串和 URL 中的秘密；保留公开正文供专业复审。 */
export function sanitizeEvidence(value, secrets = []) {
  if (Array.isArray(value)) return value.map((item) => sanitizeEvidence(item, secrets));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [
      key, SECRET_KEY.test(key) ? '[REDACTED]' : sanitizeEvidence(child, secrets),
    ]));
  }
  if (typeof value !== 'string') return value;
  let text = value.replace(/\bBearer\s+[^\s"'<>]+/gi, 'Bearer [REDACTED]')
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[REDACTED]')
    .replace(/https?:\/\/[^\s<>"')]+/g, (raw) => {
      // 报告常把 URL 放在“请求 URL：…）：说明”中；中文标点后的说明不能被 URL 构造器编码。
      const boundary = raw.search(/[）】》。，；：！？、]/u);
      const urlText = boundary < 0 ? raw : raw.slice(0, boundary);
      const suffix = boundary < 0 ? '' : raw.slice(boundary);
      try {
        const url = new URL(urlText);
        if (url.username || url.password) { url.username = ''; url.password = ''; }
        for (const key of [...url.searchParams.keys()]) if (SECRET_KEY.test(key)) url.searchParams.set(key, '[REDACTED]');
        return url.toString() + suffix;
      } catch { return '[INVALID_URL]'; }
    })
    .replace(/((?:api[_-]?key|password|secret|access[_-]?token|cookie)\s*[=:]\s*)[^\s,;"'<>）】》。，；：！？、]+/gi, '$1[REDACTED]');
  for (const secret of secrets) if (typeof secret === 'string' && secret.length >= 4) text = text.split(secret).join('[REDACTED]');
  return text;
}

/** 单次运行只写新证据文件，禁止覆盖历史回归；全部断言结束才允许标记 completed。 */
export async function runRecordedCheck({ path, report, run, captureFailure, secrets = [], write }) {
  const writer = write || (async (payload) => {
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, `${JSON.stringify(payload, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' });
  });
  try {
    await run(report);
  } catch (error) {
    report.status = 'failed';
    if (report.acceptance) report.acceptance.automation = 'failed';
    report.error = error instanceof Error ? error.message : String(error);
    report.failedStage = report.stage || 'unknown';
    try { report.failureScreenshots = await captureFailure?.() || []; }
    catch { report.screenshotError = '失败截图未能保存'; }
    await writer(sanitizeEvidence(report, secrets));
    throw new Error(`验收失败（${report.failedStage}）；脱敏证据：${path}`);
  }
  report.status = 'completed';
  report.stage = 'all_assertions_passed';
  const safeReport = sanitizeEvidence(report, secrets);
  await writer(safeReport);
  return safeReport;
}
