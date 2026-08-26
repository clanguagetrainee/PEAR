## @file test_resolveChain.py
#  赋值/调用解析链路集成测试（真实 SourceProvider + fake_lib 源码 AST 精确解析）
#  职责：验证「调用/赋值里的名字 → AST 精确 import/定义定位 → 找到定义」这条核心
#  链路，重点覆盖跨模块形态（旧实现层 2 预设同模块会失败的场景）：
#  import 别名（from ._impl import x）、点链调用（_impl.x()）、跨模块赋值别名、
#  method self 调用、多语句实现不追首个调用（含库外调用）。用真实 fake_repo +
#  SourceProvider，不碰手工 kb / FakeProvider。

import pytest

from Adjust.resolveApi import resolve_api


VERSION = '1.1.0'
SOURCE_VERSION = '1.0.0'
MOD = 'fakelib.mod'
IMPL = 'fakelib._impl'


def test_cross_module_import_forward(fake_kb, provider):
    """from ._impl import cross_new_api 后 forward 调用 -> 解析到 _impl 定义。

    旧实现层 2 会把 cross_new_api 预设为同模块（fakelib.mod.cross_new_api）而失败；
    精确 import 定位应追到 fakelib._impl.cross_new_api 并拿到定义。
    """
    src = f'{MOD}.cross_api'
    target = f'{IMPL}.cross_new_api'
    r = resolve_api(src, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'forward'
    assert r.resolved_fqn == target
    assert r.definition is not None and 'def cross_new_api' in r.definition


def test_dotted_chain_call(fake_kb, provider):
    """点链调用 _impl.dotted_target(x) -> 逐段追踪到 _impl 定义。

    根名字 _impl（from . import _impl）先定位到模块，再下钻 dotted_target 属性。
    """
    src = f'{MOD}.dotted_api'
    target = f'{IMPL}.dotted_target'
    r = resolve_api(src, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'forward'
    assert r.resolved_fqn == target
    assert r.definition is not None and 'def dotted_target' in r.definition


def test_cross_module_alias(fake_kb, provider):
    """跨模块赋值别名 cross_alias = cross_new_api() -> 追到 _impl 定义。"""
    src = f'{MOD}.cross_alias'
    target = f'{IMPL}.cross_new_api'
    r = resolve_api(src, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias'
    assert r.resolved_fqn == target


def test_method_self_call(fake_kb, provider):
    """method 内 self._impl_method(x) 指向单下划线私有 method -> direct（不追）。

    _impl_method 是单下划线开头的私有 method，单下划线拦截使其退化为 direct，
    保留 run 自身定义（`return self._impl_method(x)`），而非追到 _impl_method。
    """
    method = f'{MOD}.CrossClass.run'
    r = resolve_api(method, VERSION, SOURCE_VERSION, 'method', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == method
    assert r.definition is not None and 'self._impl_method' in r.definition


def test_multistatement_external_call_direct(fake_kb, provider):
    """多语句实现调用库外 API（np 未 import）-> kind='direct'，不追首个调用。"""
    n = f'{MOD}.external_api'
    r = resolve_api(n, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == n


def test_alias_with_args(fake_kb, provider):
    """赋值别名值带参数 new_api(x, y)：参数被丢弃，仍解析到目标。"""
    a = f'{MOD}.arg_alias'
    new = f'{MOD}.new_api'
    r = resolve_api(a, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'alias'
    assert r.resolved_fqn == new


def test_forward_dotted_full_fqn(fake_kb, provider):
    """转发目标为完整 FQN fakelib.mod.new_api(x)：绝对导入调用直接定位。"""
    src = f'{MOD}.fqn_forward'
    new = f'{MOD}.new_api'
    r = resolve_api(src, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'forward'
    assert r.resolved_fqn == new


def test_local_import_forward(fake_kb, provider):
    """函数体内局部 import 后调用：from ._impl import x 在函数内，return x(...)。

    局部 import 不产生 return、不改变控制流，仍判纯转发；名字由局部 import
    表精确解析到 fakelib._impl.cross_new_api，而非模块级绑定表（模块级只有
    from ._impl import cross_new_api，同名不冲突）。
    """
    src = f'{MOD}.local_import_api'
    target = f'{IMPL}.cross_new_api'
    r = resolve_api(src, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'forward'
    assert r.resolved_fqn == target
    assert r.definition is not None and 'def cross_new_api' in r.definition


def test_function_to_class_cross_kind_direct(fake_kb, provider):
    """function 转发到 class（跨粒度）-> kind='direct'，回退定义取自 Vs 原始定义。

    func_to_class 在 v1.1.0（vpre）转发到 Service（class，跨粒度），回退定义应取
    v1.0.0（Vs）的原始实现 `return x * 2`，而非 vpre 退化的 `return Service(x)`。
    """
    n = f'{MOD}.func_to_class'
    r = resolve_api(n, VERSION, SOURCE_VERSION, 'function', fake_kb, provider)
    assert r.kind == 'direct'
    assert r.resolved_fqn == n
    assert r.definition is not None and 'x * 2' in r.definition
    assert 'Service(x)' not in r.definition
