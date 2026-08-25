## @file test_pipeline.py
#  Pipeline 集成测试
#  职责：用 fake_repo + fake_kb 跑完整 run_pipeline，验证四态
#  （OK / NOT_DEPRECATED / NOT_FOUND / NO_CANDIDATE）与 ERROR（Vs 非法）。
#  forward 场景端到端验证最终评分与演化路径；NO_CANDIDATE 用 monkeypatch 把
#  recommend 置空触发（fake_lib 无外部别名，检索不会自然为空）。

import pytest

from Pipeline import pipeline as pipeline_mod
from Pipeline.pipeline import run_pipeline
from Tool.model import Task


def make_task(fake_repo, old_api_fqn, source='1.0.0', target='2.0.0'):
    """构造 fakelib 的 function 粒度分析任务。"""
    return Task(
        lib_name='fakelib',
        source_version=source,
        target_version=target,
        old_api_fqn=old_api_fqn,
        api_type='function',
        top_k=3,
        lib_repo_path=fake_repo,
    )


def test_forward_ok(fake_repo, fake_kb, tmp_path):
    """old_api -> forward -> new_api：最终候选为 new_api，路径评分 1.0。"""
    task = make_task(fake_repo, 'fakelib.mod.old_api')
    r = run_pipeline(task, fake_kb, cache_dir=str(tmp_path / 'cache'))
    assert r.status == 'OK'
    assert len(r.candidates) >= 1
    top = r.candidates[0]
    assert top.fqn == 'fakelib.mod.new_api'
    assert top.similarity == pytest.approx(1.0)
    assert top.evolution_path == ['fakelib.mod.new_api']


def test_not_deprecated(fake_repo, fake_kb, tmp_path):
    """new_api 全程存在 -> NOT_DEPRECATED，无候选。"""
    task = make_task(fake_repo, 'fakelib.mod.new_api')
    r = run_pipeline(task, fake_kb, cache_dir=str(tmp_path / 'cache'))
    assert r.status == 'NOT_DEPRECATED'
    assert r.candidates == []


def test_not_found(fake_repo, fake_kb, tmp_path):
    """不存在的 API -> NOT_FOUND，无候选。"""
    task = make_task(fake_repo, 'fakelib.mod.nonexistent')
    r = run_pipeline(task, fake_kb, cache_dir=str(tmp_path / 'cache'))
    assert r.status == 'NOT_FOUND'
    assert r.candidates == []


def test_no_candidate(monkeypatch, fake_repo, fake_kb, tmp_path):
    """检索恒空（monkeypatch recommend 置空）-> NO_CANDIDATE。"""
    monkeypatch.setattr(pipeline_mod, 'recommend', lambda *a, **k: [])
    task = make_task(fake_repo, 'fakelib.mod.simple_api')
    r = run_pipeline(task, fake_kb, cache_dir=str(tmp_path / 'cache'))
    assert r.status == 'NO_CANDIDATE'


def test_error_bad_version(fake_repo, fake_kb, tmp_path):
    """Vs 不在知识库版本序列 -> ERROR，error 非空。"""
    task = make_task(fake_repo, 'fakelib.mod.new_api', source='9.9.9')
    r = run_pipeline(task, fake_kb, cache_dir=str(tmp_path / 'cache'))
    assert r.status == 'ERROR'
    assert r.error is not None


def test_multihop(fake_repo, fake_kb, tmp_path):
    """多跳链 chain_api -> mid_api -> new_api2：BFS 展开第二跳，路径评分 ∏ 连乘。

    chain_api(1.1.0) 转发 mid_api；mid_api(2.0.0) 转发 new_api2；new_api2 在
    v3.0.0 存在（终点）。第一跳中 mid_api（同名）评分高于 new_api / new_api2
    （不同名同 body），故 mid_api 入队展开第二跳；最终 new_api2 有两条路径
    （单跳低分 / 两跳高分），汇总去重保留两跳高分，验证 ∏ 连乘与排序。
    """
    task = make_task(fake_repo, 'fakelib.mod.chain_api', target='3.0.0')
    r = run_pipeline(task, fake_kb, cache_dir=str(tmp_path / 'cache'))
    assert r.status == 'OK'
    assert len(r.candidates) == 2  # new_api2（两跳高分）+ new_api（单跳低分）
    top = r.candidates[0]
    assert top.fqn == 'fakelib.mod.new_api2'
    assert top.evolution_path == ['fakelib.mod.mid_api', 'fakelib.mod.new_api2']
    assert 0.0 < top.similarity < 1.0  # 两跳连乘，非 1.0
    second = r.candidates[1]
    assert second.fqn == 'fakelib.mod.new_api'
    assert second.evolution_path == ['fakelib.mod.new_api']
