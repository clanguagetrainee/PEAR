## @file test_recommend.py
#  recommend 单元测试
#  职责：纯单元（用 conftest.FakeProvider 替身），验证 recommend 的
#  排除语义（排除 original_fqn、保留 resolved_fqn）、相似度降序、top_k 截断、
#  定义缺失 / 无候选时空结果，及 repr_cache（候选 tokenize 复用）的复用与
#  None 向后兼容。

import pytest

from conftest import FakeProvider
from Tool.model import APIRecord, KnowledgeBase, ResolvedApi
from Recommend import recommend as recommend_mod
from Recommend.recommend import recommend


VERSION = '2.0.0'


def make_kb():
    """构造空知识库（recommend 的候选全集来自 provider.list_api，不依赖 kb）。"""
    kb = KnowledgeBase(lib_name='test')
    kb.versions = [VERSION]
    kb._records[VERSION] = {}
    return kb


def make_resolved(original, resolved, definition, kind='forward'):
    """构造 ResolvedApi。"""
    return ResolvedApi(original_fqn=original, resolved_fqn=resolved,
                       kind=kind, definition=definition)


def test_excludes_original_fqn():
    """被替代的旧 API（original_fqn）自身不在候选之列。"""
    old = 'fakelib.mod.old_api'
    new = 'fakelib.mod.new_api'
    defs = {
        old: 'def old_api(x):\n    return x * 2\n',
        new: 'def new_api(x):\n    return x * 2\n',
    }
    resolved = make_resolved(old, new, defs[old])
    cands = recommend(resolved, VERSION, VERSION, 'function',
                      make_kb(), FakeProvider(defs), top_k=3)
    fqns = [c.fqn for c in cands]
    assert old not in fqns
    assert new in fqns


def test_keeps_resolved_fqn_as_candidate():
    """实质 API（resolved_fqn）与查询定义相同 -> 以相似度 1.0 入选（不被误排）。"""
    old = 'fakelib.mod.old_api'
    new = 'fakelib.mod.new_api'
    body = 'def new_api(x):\n    return x * 2\n'
    defs = {new: body}
    resolved = make_resolved(old, new, body)
    cands = recommend(resolved, VERSION, VERSION, 'function',
                      make_kb(), FakeProvider(defs), top_k=3)
    assert len(cands) == 1
    assert cands[0].fqn == new
    assert cands[0].similarity == pytest.approx(1.0)


def test_sorted_desc_and_topk():
    """候选按相似度降序，且截断到 top_k。"""
    original = 'fakelib.mod.dead_api'
    a = 'fakelib.mod.a'
    b = 'fakelib.mod.b'
    c = 'fakelib.mod.c'
    query = 'def target(x):\n    return x + 1\n'
    defs = {
        a: query,                                 # 与查询完全一致 -> 1.0
        b: 'def other(y):\n    return y * 100\n',  # 明显不同 -> 低分
        c: 'def third(z):\n    return z - 5\n',
    }
    resolved = make_resolved(original, original, query, kind='direct')
    cands = recommend(resolved, VERSION, VERSION, 'function',
                      make_kb(), FakeProvider(defs), top_k=2)
    assert len(cands) == 2
    assert cands[0].fqn == a
    assert cands[0].similarity > cands[1].similarity


def test_empty_when_definition_missing():
    """原 API 定义缺失 -> 返回空（不参与比对）。"""
    resolved = make_resolved('fakelib.mod.x', 'fakelib.mod.x', None)
    cands = recommend(resolved, VERSION, VERSION, 'function',
                      make_kb(), FakeProvider({'fakelib.mod.y': 'def y():\n    pass\n'}),
                      top_k=3)
    assert cands == []


def test_empty_when_no_candidates():
    """候选全集只有原 API 自身，排除后为空 -> 返回空。"""
    original = 'fakelib.mod.only'
    defs = {original: 'def only(x):\n    return x\n'}
    resolved = make_resolved(original, original, defs[original], kind='direct')
    cands = recommend(resolved, VERSION, VERSION, 'function',
                      make_kb(), FakeProvider(defs), top_k=3)
    assert cands == []


# ---------------------------------------------------------------------------
# repr_cache 复用测试
# ---------------------------------------------------------------------------

def test_repr_cache_reuses_candidate_reprs(monkeypatch):
    """同一 (api_type, to_version) 的候选 repr 只构建一次：第二次调用只重建
    query_repr，候选 repr 从缓存复用，结果与首次一致。"""
    build_calls = []
    real_build = recommend_mod.build_repr

    def counting_build(def_text):
        build_calls.append(def_text)
        return real_build(def_text)

    monkeypatch.setattr(recommend_mod, 'build_repr', counting_build)

    defs = {
        'fakelib.mod.a': 'def a(x):\n    return x + 1\n',
        'fakelib.mod.b': 'def b(x):\n    return x + 1\n',
        'fakelib.mod.c': 'def c(x):\n    return x - 1\n',
    }
    resolved = make_resolved('fakelib.mod.old', 'fakelib.mod.old',
                             'def old(x):\n    return x + 1\n')
    cache = {}
    r1 = recommend(resolved, VERSION, VERSION, 'function', make_kb(),
                   FakeProvider(defs), top_k=3, repr_cache=cache)
    n_first = len(build_calls)
    r2 = recommend(resolved, VERSION, VERSION, 'function', make_kb(),
                   FakeProvider(defs), top_k=3, repr_cache=cache)
    n_second = len(build_calls) - n_first

    # 首次 = 3 候选 + 1 query；第二次仅重建 query，3 候选 repr 全部复用
    assert n_first == 4
    assert n_second == 1
    assert [c.fqn for c in r1] == [c.fqn for c in r2]


def test_repr_cache_none_backward_compatible():
    """不传 repr_cache（None）与传空 cache 结果一致（向后兼容）。"""
    defs = {
        'fakelib.mod.a': 'def a(x):\n    return x + 1\n',
        'fakelib.mod.b': 'def b(x):\n    return x + 1\n',
        'fakelib.mod.c': 'def c(x):\n    return x - 1\n',
    }
    resolved = make_resolved('fakelib.mod.old', 'fakelib.mod.old',
                             'def old(x):\n    return x + 1\n')
    r_none = recommend(resolved, VERSION, VERSION, 'function', make_kb(),
                       FakeProvider(defs), top_k=3)
    r_cache = recommend(resolved, VERSION, VERSION, 'function', make_kb(),
                        FakeProvider(defs), top_k=3, repr_cache={})
    assert [c.fqn for c in r_none] == [c.fqn for c in r_cache]
    assert [c.similarity for c in r_none] == pytest.approx(
        [c.similarity for c in r_cache])
