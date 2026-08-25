## @file test_direct_baseline.py
#  DirectBaseline 单元测试
#  职责：纯单元（用 conftest.FakeProvider 替身），验证 direct_baseline 的
#  直接检索语义（不排除自身、自身 1.0 排首）、相似度降序、top_k 截断、
#  查询定义缺失显式报错。注意 FakeProvider 的 get_api 与 list_api 共用
#  同一 defs 字典，故查询 fqn 本身也是候选之一。

import pytest

from conftest import FakeProvider
from Baseline.direct_baseline import direct_baseline


VS = '1.0.0'
VT = '2.0.0'


def test_query_itself_ranked_first_and_sorted():
    """候选全集不排除查询自身：自身以 1.0 排首，其余按相似度降序。"""
    q = 'fakelib.mod.q'
    a = 'fakelib.mod.a'
    b = 'fakelib.mod.b'
    defs = {
        q: 'def q(x):\n    return x + 1\n',        # 查询自身 -> 1.0
        a: 'def a(x):\n    return x + 1\n',        # 仅函数名不同 -> 高但 < 1.0
        b: 'class B:\n    def m(self):\n        pass\n',  # 结构差异大 -> 低分
    }
    cands = direct_baseline(q, VS, VT, 'function', FakeProvider(defs), top_k=3)
    assert [c.fqn for c in cands] == [q, a, b]
    assert cands[0].similarity == pytest.approx(1.0)
    assert cands[0].similarity > cands[1].similarity > cands[2].similarity


def test_topk_truncates():
    """按相似度降序且截断到 top_k。"""
    q = 'fakelib.mod.q'
    a = 'fakelib.mod.a'
    b = 'fakelib.mod.b'
    c = 'fakelib.mod.c'
    defs = {
        q: 'def q(x):\n    return x + 1\n',
        a: 'def a(x):\n    return x + 1\n',
        b: 'def b(y):\n    return y * 100\n',
        c: 'class C:\n    def m(self):\n        pass\n',
    }
    cands = direct_baseline(q, VS, VT, 'function', FakeProvider(defs), top_k=2)
    assert len(cands) == 2
    assert cands[0].fqn == q
    assert cands[0].similarity > cands[1].similarity


def test_raises_when_query_definition_missing():
    """查询在 Vs 无定义 -> 显式抛 ValueError（不静默返回空）。"""
    with pytest.raises(ValueError):
        direct_baseline('fakelib.mod.missing', VS, VT, 'function',
                        FakeProvider({'fakelib.mod.y': 'def y():\n    pass\n'}),
                        top_k=3)
