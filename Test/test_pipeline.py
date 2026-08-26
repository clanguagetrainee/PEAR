## @file test_pipeline.py
#  Pipeline 集成测试
#  职责：用 fake_repo + fake_kb 跑完整 run_pipeline，验证四态
#  （OK / NOT_DEPRECATED / NOT_FOUND / NO_CANDIDATE）与 ERROR（Vs 非法）。
#  forward 场景端到端验证最终评分与演化路径；NO_CANDIDATE 用 monkeypatch 把
#  recommend 置空触发（fake_lib 无外部别名，检索不会自然为空）。
#  另含 trace 轨迹（正常展开/展开剪枝/pop剪枝/三种死分支）与 branch-and-bound
#  剪枝正确性（结果==暴力枚举、调用减少、下界仅来自 Vt 候选）专项测试，
#  用确定性 mock 图 + monkeypatch 三个原语（get_boundary/resolve_api/recommend）。

import pytest
from types import SimpleNamespace

from Pipeline import pipeline as pipeline_mod
from Pipeline.pipeline import run_pipeline
from Tool.model import Candidate, Task


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


def test_iteration_source_version_is_pos(monkeypatch, tmp_path):
    """迭代分支 direct 候选的 source_version 取 pos（= 上一跳 vpost），非全局 Vs。

    锁定的 bug：pipeline 曾把 resolve_api 的 source_version 恒传 task.source_version
    （全局 Vs），导致迭代分支 B 在全局 Vs 尚未引入时，direct 回退定义退化为 B 自身
    vpre，而非 B 的起点版本（vpost）原始定义。本测试用确定性 mock 图断言：根节点
    a 的 source_version = pos = 'v1'（= Vs），迭代分支 b 的 source_version = pos =
    'v2'（= a 的 vpost，非全局 Vs='v1'）。
    """
    resolve_calls = []

    def fake_get_boundary(fqn, pos, target_version, kb, window):
        return SimpleNamespace(status='DEPRECATED', vpre='v1', vpost='v2')

    def fake_resolve_api(fqn, version, source_version, api_type, kb, provider,
                         visited=None):
        resolve_calls.append((fqn, source_version))
        return SimpleNamespace(kind='direct', definition='def f(): pass',
                               original_fqn=fqn, resolved_fqn=fqn)

    def fake_recommend(resolved, vpre, vpost, api_type, kb, provider, top_k,
                       repr_cache=None):
        return [Candidate(fqn=cfqn, api_type=api_type, similarity=s)
                for cfqn, s in {'a': [('b', 0.9)], 'b': [('c', 0.9)]}.get(
                    resolved.original_fqn, [])]

    monkeypatch.setattr(pipeline_mod, 'get_boundary', fake_get_boundary)
    monkeypatch.setattr(pipeline_mod, 'resolve_api', fake_resolve_api)
    monkeypatch.setattr(pipeline_mod, 'recommend', fake_recommend)

    class FakeKb:
        versions = ['v1', 'v2', 'v3']

        def exists(self, fqn, version):
            return fqn == 'c' and version == 'v3'

    task = Task(lib_name='x', source_version='v1', target_version='v3',
                old_api_fqn='a', api_type='function', top_k=3,
                lib_repo_path=str(tmp_path / 'repo'))
    r = run_pipeline(task, FakeKb(), cache_dir=str(tmp_path / 'cache'),
                     provider=SimpleNamespace())

    assert r.status == 'OK'
    assert resolve_calls == [('a', 'v1'), ('b', 'v2')]


# ---------------------------------------------------------------------------
# trace 轨迹专项测试（确定性 mock 图，monkeypatch 三个原语）
# ---------------------------------------------------------------------------

# 扇出图：a 分叉 b/c，b 分叉 d/e，e 分叉 h/i，c 分叉 f/g，g 分叉 j/k，
# i/k 各引出一个深叶（l/m）。Vt 含 d/f/h/j/l/m 六个终点。该图每个 Vt 候选
# 仅一条路径，故「最高分路径 == 入 final 时的路径分数」，便于精确断言下界。
_FAN_GRAPH = {
    'a': [('b', 0.9), ('c', 0.8)],
    'b': [('d', 0.9), ('e', 0.9)],
    'c': [('f', 0.7), ('g', 0.9)],
    'e': [('h', 0.9), ('i', 0.05)],
    'g': [('j', 0.9), ('k', 0.05)],
    'i': [('l', 0.9)],
    'k': [('m', 0.9)],
}
_FAN_VT = {'d', 'f', 'h', 'j', 'l', 'm'}


def _run_mock(graph, in_vt, top_k, monkeypatch, tmp_path):
    """用确定性 mock 图 + monkeypatch 三个原语跑 run_pipeline，返回 (result, calls)。

    输入参数：
        graph (dict)：fqn -> [(候选 fqn, 单跳相似度), ...]。
        in_vt (set)：在目标版本 v3 存在的 fqn 集合。
        top_k (int)：top_k。
        monkeypatch：pytest monkeypatch fixture（自动恢复被替换的原语）。
        tmp_path：pytest 临时目录（作 repo/cache 路径占位）。
    返回值：
        (Result, List[str])：run_pipeline 结果，及 recommend 被调用的
        original_fqn 列表（按调用顺序）。
    """
    calls = []

    def fake_get_boundary(fqn, pos, target_version, kb, window):
        if 'notfound' in fqn:
            return SimpleNamespace(status='NOT_FOUND', vpre=None, vpost=None)
        return SimpleNamespace(status='DEPRECATED', vpre='v1', vpost='v2')

    def fake_resolve_api(fqn, version, api_type, kb, provider, visited=None):
        if 'aliasx' in fqn:
            return SimpleNamespace(kind='alias_external', definition=None,
                                   original_fqn=fqn, resolved_fqn='ext.mod.x')
        return SimpleNamespace(kind='new_api', definition='def f(): pass',
                               original_fqn=fqn, resolved_fqn=fqn)

    def fake_recommend(resolved, vpre, vpost, api_type, kb, provider, top_k,
                       repr_cache=None):
        calls.append(resolved.original_fqn)
        return [Candidate(fqn=cfqn, api_type=api_type, similarity=s)
                for cfqn, s in graph.get(resolved.original_fqn, [])]

    monkeypatch.setattr(pipeline_mod, 'get_boundary', fake_get_boundary)
    monkeypatch.setattr(pipeline_mod, 'resolve_api', fake_resolve_api)
    monkeypatch.setattr(pipeline_mod, 'recommend', fake_recommend)

    class FakeKb:
        versions = ['v1', 'v2', 'v3']

        def exists(self, fqn, version):
            return fqn in in_vt and version == 'v3'

    task = Task(lib_name='x', source_version='v1', target_version='v3',
                old_api_fqn='a', api_type='function', top_k=top_k,
                lib_repo_path=str(tmp_path / 'repo'))
    result = run_pipeline(task, FakeKb(), cache_dir=str(tmp_path / 'cache'),
                          provider=SimpleNamespace())
    return result, calls


def _brute_force(graph, in_vt, top_k):
    """暴力枚举所有 a→Vt 路径，每个 Vt fqn 取最高分，返回 (top_k fqn 列表, best)。

    输入参数：
        graph (dict)：mock 图。
        in_vt (set)：Vt 存在集合。
        top_k (int)：top_k。
    返回值：
        (List[str], Dict[str, float])：top_k fqn 列表，及 {Vt fqn: 最高路径分}。
    """
    best = {}

    def dfs(fqn, scores):
        for cfqn, sim in graph.get(fqn, []):
            ns = scores + [sim]
            score = 1.0
            for s in ns:
                score *= s
            if cfqn in in_vt:
                if cfqn not in best or score > best[cfqn]:
                    best[cfqn] = score
            else:
                dfs(cfqn, ns)

    dfs('a', [])
    ranked = [f for f, _ in sorted(best.items(), key=lambda kv: -kv[1])[:top_k]]
    return ranked, best


def _count_full_expand(graph, in_vt):
    """无剪枝时全部可达中间节点数（≈ recommend 全展开的调用上限）。"""
    from collections import deque
    queue = deque(['a'])
    visited = set()
    n = 0
    while queue:
        fqn = queue.popleft()
        if fqn in visited:
            continue
        visited.add(fqn)
        n += 1
        for cfqn, _sim in graph.get(fqn, []):
            if cfqn not in in_vt:
                queue.append(cfqn)
    return n


def test_trace_normal_and_expand_prune(monkeypatch, tmp_path):
    """正常展开 + 展开剪枝：trace 记录各节点 fqn/candidates/entered_final/
    entered_iter/pruned/prune_bound；下界 0.729 来自 Vt 候选 h。"""
    result, calls = _run_mock(_FAN_GRAPH, _FAN_VT, top_k=2,
                              monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result.status == 'OK'
    assert [c.fqn for c in result.candidates] == ['d', 'h']
    by = {t['fqn']: t for t in result.trace}
    assert set(by) == {'a', 'b', 'e', 'c'}
    assert by['a']['entered_iter'] == ['b', 'c']
    assert by['a']['entered_final'] == [] and by['a']['pruned'] == []
    assert by['a']['prune_bound'] is None
    assert by['a']['candidates'] == [
        {'fqn': 'b', 'similarity': 0.9}, {'fqn': 'c', 'similarity': 0.8}]
    assert by['b']['entered_final'] == ['d'] and by['b']['entered_iter'] == ['e']
    assert by['e']['entered_final'] == ['h'] and by['e']['pruned'] == ['i']
    assert by['e']['prune_bound'] == pytest.approx(0.729)
    assert by['c']['entered_final'] == ['f'] and by['c']['pruned'] == ['g']
    # recommend 只展开 a/b/e/c 四个中间节点（d/f/h 终点不展开，i/g 剪枝不展开）
    assert calls == ['a', 'b', 'e', 'c']


def test_trace_pop_prune(monkeypatch, tmp_path):
    """pop 剪枝：路径分数低于已满下界时整分支不展开，记录 dead_reason='pruned_at_pop'。"""
    graph = {
        'a': [('b', 0.9), ('c', 0.5)],
        'b': [('d', 0.9), ('e', 0.9)],
        'c': [('f', 0.9)],
    }
    result, calls = _run_mock(graph, {'d', 'e', 'f'}, top_k=2,
                              monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result.status == 'OK'
    assert [c.fqn for c in result.candidates] == ['d', 'e']
    by = {t['fqn']: t for t in result.trace}
    assert set(by) == {'a', 'b', 'c'}
    assert by['c']['dead_reason'] == 'pruned_at_pop'
    assert by['c']['prune_bound'] == pytest.approx(0.81)


def test_trace_dead_branches(monkeypatch, tmp_path):
    """三种死分支：alias_external / empty_recommend / not_found，均记录 dead_reason。"""
    graph = {
        'a': [('aliasx', 0.9), ('empty', 0.8), ('notfound', 0.7), ('b', 0.6)],
        'b': [('d', 0.9)],
    }
    result, calls = _run_mock(graph, {'d'}, top_k=2,
                              monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result.status == 'OK'
    assert [c.fqn for c in result.candidates] == ['d']
    by = {t['fqn']: t for t in result.trace}
    assert set(by) == {'a', 'aliasx', 'empty', 'notfound', 'b'}
    assert by['aliasx']['dead_reason'] == 'alias_external'
    assert by['empty']['dead_reason'] == 'empty_recommend'
    assert by['notfound']['dead_reason'] == 'not_found'
    assert by['a']['entered_iter'] == ['aliasx', 'empty', 'notfound', 'b']


# ---------------------------------------------------------------------------
# branch-and-bound 剪枝正确性专项测试
# ---------------------------------------------------------------------------

def test_prune_matches_bruteforce(monkeypatch, tmp_path):
    """剪枝版 top_k 与暴力枚举 ground truth 完全一致（不多剪、不少剪）。"""
    result, _ = _run_mock(_FAN_GRAPH, _FAN_VT, top_k=2,
                          monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result.status == 'OK'
    got = [c.fqn for c in result.candidates]
    truth, _ = _brute_force(_FAN_GRAPH, _FAN_VT, top_k=2)
    assert got == truth


def test_prune_reduces_recommend_calls(monkeypatch, tmp_path):
    """剪枝确实减少了 recommend 调用次数（< 无剪枝全展开节点数）。"""
    _, calls = _run_mock(_FAN_GRAPH, _FAN_VT, top_k=2,
                         monkeypatch=monkeypatch, tmp_path=tmp_path)
    full = _count_full_expand(_FAN_GRAPH, _FAN_VT)
    assert len(calls) < full


def test_prune_bound_only_from_vt_candidates(monkeypatch, tmp_path):
    """剪枝下界必须来自 Vt 候选的路径分数，绝不用中间节点分数（防误剪）。

    _FAN_GRAPH 中中间节点 b 路径分数 0.9 高于所有 Vt 候选（d=0.81），若误用 b
    做下界会把 d 剪掉。断言 trace 里所有 prune_bound 都属于 Vt 候选分数集合，
    且结果包含 d（0.81），证明下界未取中间节点 0.9。
    """
    result, _ = _run_mock(_FAN_GRAPH, _FAN_VT, top_k=2,
                          monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert result.status == 'OK'
    _, best = _brute_force(_FAN_GRAPH, _FAN_VT, top_k=2)
    vt_scores = set(best.values())
    bounds = [t['prune_bound'] for t in result.trace
              if t['prune_bound'] is not None]
    assert bounds, "应发生至少一次剪枝"
    for b in bounds:
        assert any(abs(b - s) < 1e-6 for s in vt_scores), \
            f"下界 {b} 不是任何 Vt 候选的路径分数"
    assert 'd' in [c.fqn for c in result.candidates], "中间节点高分不应误剪 d"


def test_result_to_dict(fake_repo, fake_kb, tmp_path):
    """Result.to_dict() 产出 task/status/candidates/trace 结构，candidates 为对象列表。"""
    task = make_task(fake_repo, 'fakelib.mod.old_api')
    r = run_pipeline(task, fake_kb, cache_dir=str(tmp_path / 'cache'))
    d = r.to_dict()
    assert d["status"] == r.status == 'OK'
    assert d["error"] is None
    assert d["task"]["old_api_fqn"] == 'fakelib.mod.old_api'
    assert d["task"]["api_type"] == 'function'
    assert d["task"]["top_k"] == 3
    # candidates 与 Result.candidates 一一对应，且转成对象 dict
    assert len(d["candidates"]) == len(r.candidates)
    for cd, c in zip(d["candidates"], r.candidates):
        assert cd == {
            "fqn": c.fqn,
            "api_type": c.api_type,
            "similarity": c.similarity,
            "evolution_path": list(c.evolution_path),
            "local_scores": list(c.local_scores),
        }
    assert d["trace"] == r.trace
