## @file test_sourceProvider.py
#  SourceProvider 集成测试
#  职责：用 fake_repo（多版本假库 git 仓库）验证 SourceProvider 的整批生成 /
#  定义读取 / 候选全集列出 / 幂等 / 版本演化差异。真库（pandas 等）不在此测。

import os

import pytest

from Tool.tool import SourceProvider


def _provider(fake_repo, tmp_path):
    """构造 SourceProvider，worktree 根隔离到 tmp_path，避免污染默认目录。"""
    return SourceProvider('fakelib', fake_repo, str(tmp_path / 'cache'),
                          worktrees_root=str(tmp_path / 'wt'))


def test_list_api_function_v1(fake_repo, tmp_path):
    """v1.0.0 function 粒度候选全集包含全部模块级函数。"""
    p = _provider(fake_repo, tmp_path)
    try:
        fqns = p.list_api('function', '1.0.0')
        for expected in ('fakelib.mod.new_api', 'fakelib.mod.old_api',
                         'fakelib.mod.simple_api', 'fakelib.mod.aliased',
                         'fakelib.mod.nested_api'):
            assert expected in fqns
    finally:
        p.close()


def test_get_api_definition(fake_repo, tmp_path):
    """get_api 返回含 def 的完整定义文本。"""
    p = _provider(fake_repo, tmp_path)
    try:
        text = p.get_api('fakelib.mod.old_api', 'function', '1.0.0')
        assert text is not None
        assert 'def old_api' in text
    finally:
        p.close()


def test_batch_idempotent(fake_repo, tmp_path):
    """整批生成幂等：第一次生成写 .done，第二次 no-op 不抛错。"""
    p = _provider(fake_repo, tmp_path)
    try:
        p.ensure_batch('function', '1.0.0')
        done = os.path.join(p._batch_dir('function', '1.0.0'), '.done')
        assert os.path.exists(done)
        p.ensure_batch('function', '1.0.0')  # 已生成，直接返回
    finally:
        p.close()


def test_version_evolution(fake_repo, tmp_path):
    """版本演化差异：simple_api 在 v1.1.0 消失，aliased 变赋值别名后不再是 def。"""
    p = _provider(fake_repo, tmp_path)
    try:
        v1 = p.list_api('function', '1.0.0')
        v11 = p.list_api('function', '1.1.0')
        v2 = p.list_api('function', '2.0.0')
        assert 'fakelib.mod.simple_api' in v1
        assert 'fakelib.mod.simple_api' not in v11
        assert 'fakelib.mod.aliased' in v1
        assert 'fakelib.mod.aliased' not in v11  # 赋值别名不产出完整定义
        assert v2 == ['fakelib.mod.mid_api', 'fakelib.mod.new_api',
                      'fakelib.mod.new_api2']  # v2 只剩多跳链上的 3 个 API
    finally:
        p.close()


def test_class_granularity(fake_repo, tmp_path):
    """class 粒度：v1.0.0 收集类本身定义（含 Service 类）。"""
    p = _provider(fake_repo, tmp_path)
    try:
        classes = p.list_api('class', '1.0.0')
        assert 'fakelib.mod.Service' in classes
        text = p.get_api('fakelib.mod.Service', 'class', '1.0.0')
        assert text is not None and 'class Service' in text
    finally:
        p.close()


def test_method_granularity(fake_repo, tmp_path):
    """method 粒度：收集类内方法，排除 __init__/__new__/__call__。"""
    p = _provider(fake_repo, tmp_path)
    try:
        methods = p.list_api('method', '1.0.0')
        assert 'fakelib.mod.Service.process' in methods
        assert 'fakelib.mod.Service.__init__' not in methods
        text = p.get_api('fakelib.mod.Service.process', 'method', '1.0.0')
        assert text is not None and 'def process' in text
    finally:
        p.close()
