"""
@Time       : 2026/10/03
@Author     : zhanglp8181
@File       : test_staffdeck_sd8_benchmark.py
@CallChain  : pytest → SD8 固定基准 → 真实知识检索 → 独立指标重算
@Description: 验证合成事实标签、真实分层规模、预热排除和原始样本指标一致性。
"""

import gzip
import importlib.util
import json
from pathlib import Path

from sqlmodel import SQLModel, create_engine


def _benchmark():
    """加载自有脚本；不读取 otherpro 或真实配置与用户内容。"""
    path = Path(__file__).resolve().parents[2] / "scripts/benchmark_staffdeck_knowledge.py"
    spec = importlib.util.spec_from_file_location("sd8_benchmark", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_dataset_and_human_labels_are_deterministic():
    """相同规模生成相同摘要，主文档数量真实为1K/10K，控制标签不取自评分器。"""
    bench = _benchmark()
    for scale in (1000, 10000):
        data = bench.dataset(scale)
        assert bench.digest(data) == bench.digest(bench.dataset(scale))
        assert sum(r["knowledge_base_version_id"] == "main" for r in
                   data["rows"]["knowledge_documents"]) == scale
        assert sum(r["knowledge_base_version_id"] == "control" for r in
                   data["rows"]["knowledge_concepts"]) == 121
        assert sum(r["knowledge_base_version_id"] == "doccontrol" for r in
                   data["rows"]["knowledge_documents"]) == 146
    assert bench.labels()[0]["concepts"] == ["orchid-old"]
    assert len(next(q for q in bench.labels() if q["id"] == "complete")["aspects"]) == 2


def test_quantiles_pool_original_samples_and_exclude_warmup():
    """nearest-rank分位数包含慢查询，不平均轮次或删除极端值。"""
    bench = _benchmark()
    assert bench.quantile([1, 2, 3, 4, 100], .5) == 3
    assert bench.quantile([1, 2, 3, 4, 100], .95) == 100
    assert bench.recall(["a", "b"], ["b", "c"]) == .5
    assert bench.recall([], ["c"]) is None


def test_actual_sqlite_samples_recompute_and_preserve_hard_contracts(tmp_path):
    """在文件SQLite执行完整查询集，重算原样本并验证候选、版本和无答案硬边界。"""
    bench = _benchmark()
    engine = create_engine("sqlite:///" + str(tmp_path / "benchmark.sqlite3"))
    SQLModel.metadata.create_all(engine)
    try:
        output = tmp_path / "samples"
        result = bench.run(engine, output, 146, rounds=1)
        with gzip.open(output / "samples.jsonl.gz", "rt") as stream:
            samples = [json.loads(line) for line in stream]
        assert len(samples) == 20 and result["measured"] == 10
        assert bench.summarize(samples, bench.labels()) == result
        for rows in result["per_query"].values():
            assert all(r["unauthorized"] == 0 and r["no_answer_false_citations"] == 0 for r in rows)
        assert result["per_query"]["old-document-146"][0]["chunk_recall_at_8"] == 1
        assert result["per_query"]["complete"][0]["completeness"] == 1
    finally:
        engine.dispose()


def test_query_failure_is_preserved_without_losing_later_samples(tmp_path, monkeypatch):
    """首个真实查询异常仍保存身份/时延/错误摘要，后续查询照常采样且汇总明确失败。"""
    bench = _benchmark()
    original = bench.MeasuredService.search
    calls = 0

    def fail_first(self, *args, **kwargs):
        """只使首次检索失效，保留其余实际生产检索作为继续采样对照。"""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("SYNTHETIC_QUERY_FAILURE")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(bench.MeasuredService, "search", fail_first)
    engine = create_engine("sqlite:///" + str(tmp_path / "failure.sqlite3"))
    SQLModel.metadata.create_all(engine)
    try:
        output = tmp_path / "samples"
        result = bench.run(engine, output, 146, rounds=1)
        with gzip.open(output / "samples.jsonl.gz", "rt") as stream:
            samples = [json.loads(line) for line in stream]
        assert result["errors"] == 1 and result["samples"] == 20
        assert samples[0]["error"]["type"] == "RuntimeError"
        assert samples[-1]["error"] is None
    finally:
        engine.dispose()
