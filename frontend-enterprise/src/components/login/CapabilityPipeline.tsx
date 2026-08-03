import type { CSSProperties } from 'react';

const CAPABILITY_SOURCES = [
  { code: 'K', label: '岗位知识', detail: '制度、案例与岗位方法' },
  { code: 'S', label: '业务 SOP', detail: '把标准流程带入执行' },
  { code: 'M', label: '工作记忆', detail: '在任务中持续积累经验' },
  { code: 'T', label: '业务工具', detail: '连接系统并完成真实操作' },
] as const;

/** Real experience flowing into a governed, traceable digital employee. */
export default function CapabilityPipeline() {
  return (
    <section className="capability-pipeline" aria-labelledby="capability-pipeline-title">
      <div className="capability-pipeline-grid" aria-hidden="true" />
      <header className="capability-pipeline-head">
        <div>
          <span className="capability-pipeline-kicker">CAPABILITY BLUEPRINT</span>
          <h2 id="capability-pipeline-title">让岗位经验，成为可执行能力</h2>
        </div>
        <span className="capability-pipeline-status"><i /> 在线协同</span>
      </header>

      <div className="capability-pipeline-flow">
        <div className="capability-source-rail">
          <span className="capability-rail-label">经验输入</span>
          <ol>
            {CAPABILITY_SOURCES.map((source, index) => (
              <li key={source.code} style={{ '--pipeline-index': index } as CSSProperties}>
                <span className="capability-source-code">{source.code}</span>
                <span>
                  <strong>{source.label}</strong>
                  <small>{source.detail}</small>
                </span>
              </li>
            ))}
          </ol>
        </div>

        <div className="capability-flow-core" aria-hidden="true">
          <span className="capability-flow-line" />
          <span className="capability-flow-pulse" />
        </div>

        <div className="capability-twin-card">
          <div className="capability-twin-orbit" aria-hidden="true"><span /><span /></div>
          <span className="capability-twin-label">序伴员工</span>
          <strong>理解 · 执行 · 沉淀</strong>
          <p>在权限边界内协同工作，把每一次任务变成下一次更可靠的经验。</p>
          <div className="capability-twin-metrics">
            <span><b>01</b> 经验可复用</span>
            <span><b>02</b> 执行有边界</span>
          </div>
        </div>
      </div>

      <footer className="capability-pipeline-assurance">
        <span><i aria-hidden="true">✓</i> 全链路可追溯</span>
        <span><i aria-hidden="true">✓</i> 关键节点人工确认</span>
      </footer>
    </section>
  );
}
