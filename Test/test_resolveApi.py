## @file test_resolveApi.py
#  resolveApi 集成测试（真实 SourceProvider + fake_lib 源码 AST 精确解析）
#  职责：用真实 fake_repo（git 仓库）+ SourceProvider（module_ast / locate_module）
#  验证 resolve_api 的七种 kind（direct / forward / nested / alias / alias_external /
#  nested_external / unknown）、链式更正（alias -> forward）与 visited 防环。
#  取代原「FakeProvider + 手工 kb」纯单元——名字解析已改为 AST 精确 import/定义
#  定位，不再依赖 kb.alias_of 与同名近似匹配。

import pytest

from Adjust.resolveApi import resolve_api


VERSION = '1.1.0'
MOD = 'fakelib.mod'


def test_direct(fake_kb, provider):
    """定义无调用 -> kind='direct'，resolved_fqn 为自身。"""
    fqn = f'{MOD}._helper'
    r = resolve_api(fqn, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == fqn
    assert r.definition is not None


def test_forward(fake_kb, provider):
    """纯转发 -> kind='forward'，resolved_fqn 为被调 API。"""
    old = f'{MOD}.old_api'
    new = f'{MOD}.new_api'
    r = resolve_api(old, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'forward'
    assert r.resolved_fqn == new


def test_nested(fake_kb, provider):
    """内含调用非纯转发 -> kind='nested'，resolved_fqn 为首个被调 API。"""
    n = f'{MOD}.nested_api'
    helper = f'{MOD}._helper'
    r = resolve_api(n, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'nested'
    assert r.resolved_fqn == helper


def test_alias(fake_kb, provider):
    """赋值别名 -> kind='alias'，resolved_fqn 为赋值目标 API。"""
    a = f'{MOD}.aliased'
    new = f'{MOD}.new_api'
    r = resolve_api(a, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias'
    assert r.resolved_fqn == new


def test_alias_external(fake_kb, provider):
    """别名指向非本库 API（np.array，np 未 import）-> kind='alias_external'。"""
    a = f'{MOD}.external_alias'
    r = resolve_api(a, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias_external'
    assert r.resolved_fqn == a


def test_nested_external(fake_kb, provider):
    """嵌套调用目标是外部 API -> kind='nested_external'，退回自身。"""
    n = f'{MOD}.external_api'
    r = resolve_api(n, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'nested_external'
    assert r.resolved_fqn == n


def test_unknown(fake_kb, provider):
    """定义不存在 -> kind='unknown'，definition 为 None。"""
    fqn = f'{MOD}.nonexistent'
    r = resolve_api(fqn, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'unknown'
    assert r.definition is None


def test_class_direct(fake_kb, provider):
    """class 粒度无转发/嵌套语义 -> kind='direct'。"""
    fqn = f'{MOD}.CrossClass'
    r = resolve_api(fqn, VERSION, 'class', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == fqn


def test_alias_to_forward_chain(fake_kb, provider):
    """别名 -> 转发 链式更正，最终 resolved_fqn 为链条末端，kind 记最外层 alias。"""
    a = f'{MOD}.chain_alias'
    new = f'{MOD}.new_api'
    r = resolve_api(a, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias'
    assert r.resolved_fqn == new


def test_cycle_guarded(fake_kb, provider):
    """别名互相指向 -> visited 防环，kind='unknown'。"""
    a = f'{MOD}.cycle_a'
    r = resolve_api(a, VERSION, 'function', fake_kb, provider)
    assert r.kind == 'unknown'
