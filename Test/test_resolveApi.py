## @file test_resolveApi.py
#  resolveApi 集成测试（真实 SourceProvider + fake_lib 源码 AST 精确解析）
#  职责：用真实 fake_repo（git 仓库）+ SourceProvider（module_ast / locate_module）
#  验证 resolve_api 的五种 kind（direct / forward / alias / alias_external /
#  unknown）、链式更正（alias -> forward）与 visited 防环。
#  取代原「FakeProvider + 手工 kb」纯单元——名字解析已改为 AST 精确 import/定义
#  定位，不再依赖 kb.alias_of 与同名近似匹配。

import pytest

from Adjust.resolveApi import resolve_api


VERSION = '1.1.0'
SOURCE_VERSION = '1.0.0'
MOD = 'fakelib.mod'


def test_direct(fake_kb, provider):
    """定义无调用 -> kind='direct'，resolved_fqn 为自身。"""
    fqn = f'{MOD}._helper'
    r = resolve_api(fqn, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == fqn
    assert r.definition is not None


def test_forward(fake_kb, provider):
    """纯转发 -> kind='forward'，resolved_fqn 为被调 API。"""
    old = f'{MOD}.old_api'
    new = f'{MOD}.new_api'
    r = resolve_api(old, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'forward'
    assert r.resolved_fqn == new


def test_multistatement_direct(fake_kb, provider):
    """多语句实现（非纯转发）-> kind='direct'，回退定义取自 Vs 原始定义。

    nested_api 在 v1.1.0（vpre）为多语句（y = _helper(x); return y + 1），direct
    回退定义应取 v1.0.0（Vs）的原始实现 `return x * 2`，而非 vpre 的多语句实现。
    """
    n = f'{MOD}.nested_api'
    r = resolve_api(n, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == n
    assert r.definition is not None and 'x * 2' in r.definition
    assert '_helper' not in r.definition


def test_alias(fake_kb, provider):
    """赋值别名 -> kind='alias'，resolved_fqn 为赋值目标 API。"""
    a = f'{MOD}.aliased'
    new = f'{MOD}.new_api'
    r = resolve_api(a, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias'
    assert r.resolved_fqn == new


def test_alias_external(fake_kb, provider):
    """别名指向非本库 API（np.array，np 未 import）-> kind='alias_external'。"""
    a = f'{MOD}.external_alias'
    r = resolve_api(a, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias_external'
    assert r.resolved_fqn == a


def test_external_call_direct(fake_kb, provider):
    """多语句实现调用库外 API -> kind='direct'，保留自身定义（不再追首个调用）。"""
    n = f'{MOD}.external_api'
    r = resolve_api(n, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == n


def test_unknown(fake_kb, provider):
    """定义不存在 -> kind='unknown'，definition 为 None。"""
    fqn = f'{MOD}.nonexistent'
    r = resolve_api(fqn, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'unknown'
    assert r.definition is None


def test_class_direct(fake_kb, provider):
    """class 粒度无转发/嵌套语义 -> kind='direct'。"""
    fqn = f'{MOD}.CrossClass'
    r = resolve_api(fqn, VERSION, SOURCE_VERSION, 'class', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == fqn


def test_class_direct_uses_vs_definition(fake_kb, provider):
    """class 粒度 direct -> 定义取自 Vs 原始实现。

    Service.process 在 v1.0.0（Vs）为 `return x * 3`，v1.1.0（vpre）为 `return x * 2`；
    class 的 direct 回退定义应取 Vs 的 `x * 3`，而非 vpre 的 `x * 2`。
    """
    fqn = f'{MOD}.Service'
    r = resolve_api(fqn, VERSION, SOURCE_VERSION, 'class', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == fqn
    assert r.definition is not None and 'x * 3' in r.definition
    assert 'x * 2' not in r.definition


def test_alias_to_forward_chain(fake_kb, provider):
    """别名 -> 转发 链式更正，最终 resolved_fqn 为链条末端，kind 记最外层 alias。"""
    a = f'{MOD}.chain_alias'
    new = f'{MOD}.new_api'
    r = resolve_api(a, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias'
    assert r.resolved_fqn == new


def test_cycle_guarded(fake_kb, provider):
    """别名互相指向 -> visited 防环，kind='unknown'。"""
    a = f'{MOD}.cycle_a'
    r = resolve_api(a, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'unknown'


def test_forward_to_single_underscore_is_direct(fake_kb, provider):
    """纯转发指向单下划线私有名 -> kind='direct'，定义取 Vs 原始实现。

    fwd_private 在 v1.1.0（vpre）为 `return _helper(x)`（纯转发，目标 _helper 是
    单下划线私有名）；单下划线拦截应使其退化为 direct，定义取 v1.0.0（Vs）的原始
    实现 `return x * 3`，而非追到 _helper（`return x * 2`）。
    """
    fqn = f'{MOD}.fwd_private'
    r = resolve_api(fqn, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == fqn
    assert r.definition is not None and 'x * 3' in r.definition
    assert 'x * 2' not in r.definition


def test_forward_to_double_underscore_not_blocked(fake_kb, provider):
    """纯转发指向双下划线私有名 -> 不拦截，kind='forward'。

    fwd_dunder 在 v1.1.0 为 `return __dunder(x)`（目标 __dunder 是双下划线，非单
    下划线），应正常 forward 追到 __dunder。
    """
    fqn = f'{MOD}.fwd_dunder'
    r = resolve_api(fqn, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'forward'
    assert r.resolved_fqn == f'{MOD}.__dunder'
