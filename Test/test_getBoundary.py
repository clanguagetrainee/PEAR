## @file test_getBoundary.py
#  getBoundary 单元测试
#  职责：纯单元（不碰 git），构造 KnowledgeBase 验证 get_boundary 的三态边界
#  （DEPRECATED / NOT_DEPRECATED / NOT_FOUND）、含别名行的边界、以及参数校验。

import pytest

from Tool.model import APIRecord, KnowledgeBase
from Adjust.getBoundary import get_boundary


def _key(v):
    return tuple(int(x) for x in v.split('.'))


def make_kb(records):
    """构造 KnowledgeBase。

    输入参数：
        records (dict)：{version: {fqn: APIRecord}}。
    返回值：
        KnowledgeBase。
    """
    kb = KnowledgeBase(lib_name='test')
    for v, recs in records.items():
        kb._records[v] = dict(recs)
    kb.versions = sorted(records, key=_key)
    return kb


VERSIONS = ['1.0.0', '1.1.0', '2.0.0']


def test_deprecated_first_boundary():
    """fqn 在中间版本消失 -> DEPRECATED，vpre/vpost 为消失相邻对。"""
    kb = make_kb({
        '1.0.0': {'a.mod.f': APIRecord('a.mod.f')},
        '1.1.0': {'a.mod.f': APIRecord('a.mod.f')},
        '2.0.0': {},  # 消失
    })
    r = get_boundary('a.mod.f', '1.0.0', '2.0.0', kb, VERSIONS)
    assert r.status == 'DEPRECATED'
    assert r.vpre == '1.1.0'
    assert r.vpost == '2.0.0'


def test_deprecated_alias_line_counts():
    """fqn 以别名行（A:）存在的版本也算存在，边界落在别名也消失处。"""
    kb = make_kb({
        '1.0.0': {'a.mod.f': APIRecord('a.mod.f', signature='(x)')},
        '1.1.0': {'a.mod.f': APIRecord('a.mod.f', alias_of='a.mod.g()')},  # 别名形态
        '2.0.0': {},
    })
    r = get_boundary('a.mod.f', '1.0.0', '2.0.0', kb, VERSIONS)
    assert r.status == 'DEPRECATED'
    assert (r.vpre, r.vpost) == ('1.1.0', '2.0.0')


def test_not_deprecated():
    """fqn 全程存在 -> NOT_DEPRECATED。"""
    kb = make_kb({
        '1.0.0': {'a.mod.f': APIRecord('a.mod.f')},
        '1.1.0': {'a.mod.f': APIRecord('a.mod.f')},
        '2.0.0': {'a.mod.f': APIRecord('a.mod.f')},
    })
    r = get_boundary('a.mod.f', '1.0.0', '2.0.0', kb, VERSIONS)
    assert r.status == 'NOT_DEPRECATED'
    assert r.vpre is None and r.vpost is None


def test_not_found():
    """fqn 在 start 即不存在 -> NOT_FOUND。"""
    kb = make_kb({
        '1.0.0': {},
        '1.1.0': {'a.mod.f': APIRecord('a.mod.f')},
        '2.0.0': {'a.mod.f': APIRecord('a.mod.f')},
    })
    r = get_boundary('a.mod.f', '1.0.0', '2.0.0', kb, VERSIONS)
    assert r.status == 'NOT_FOUND'


def test_start_version_not_in_versions():
    """start_version 不在版本序列 -> ValueError。"""
    kb = make_kb({'1.0.0': {'a.mod.f': APIRecord('a.mod.f')}})
    with pytest.raises(ValueError):
        get_boundary('a.mod.f', '0.9.0', '2.0.0', kb, VERSIONS)


def test_reentrant_from_start():
    """可重入：从中间版本 start 查起，只查 start 之后的边界。"""
    kb = make_kb({
        '1.0.0': {'a.mod.f': APIRecord('a.mod.f')},
        '1.1.0': {},
        '2.0.0': {},
    })
    r = get_boundary('a.mod.f', '1.0.0', '2.0.0', kb, VERSIONS)
    assert (r.status, r.vpre, r.vpost) == ('DEPRECATED', '1.0.0', '1.1.0')
