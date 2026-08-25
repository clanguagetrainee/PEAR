## @file make_fakelib.py
#  测试夹具：构造多版本假库 git 仓库
#  职责：在指定目录生成一个本地 git 仓库（含 fakelib 包与 4 个 tag），预置 API
#  演化形态：
#   - 单跳 forward（old_api→new_api）、alias（aliased→new_api 赋值别名）、nested
#     （nested_api→_helper）、direct（simple_api 直接删除）、not_deprecated（new_api）。
#   - 多跳链 chain_api→mid_api→new_api2（跨 4 个版本，验证 BFS 第二跳 + ∏ 连乘 +
#     汇总去重）。
#   - 跨模块形态（fakelib/_impl.py）：import 别名、点链调用、跨模块赋值别名、
#     函数内局部 import、method self 调用、库外 API。
#   - 带 __init__ 的类 Service（验证 class/method 粒度 SourceProvider 收集与
#     __init__/__new__/__call__ 排除）。
#  用法：python make_fakelib.py --out /tmp/pear_fakelib

import argparse
import os
import shutil
import subprocess
from typing import Dict, List, Tuple


## 四个版本各自源码文件内容 {相对包内路径: 源码文本}
_VERSIONS: List[Tuple[str, Dict[str, str]]] = [
    # v1.0.0：各 API 均为普通定义；Service 类带 __init__（class/method 粒度测试）
    ("v1.0.0", {
        "mod.py": '''\
def old_api(x):
    """旧 API，将转发到 new_api"""
    return x * 2


def aliased(x):
    """将变成赋值别名"""
    return x * 2


def nested_api(x):
    """嵌套调用（非纯转发）"""
    y = _helper(x)
    return y + 1


def _helper(x):
    return x * 2


def simple_api(x):
    """普通定义，下个版本直接删除"""
    return x * 3


def new_api(x):
    """全程存在"""
    return x * 2


def chain_api(x):
    """多跳链起点"""
    return x * 2


def mid_api(x):
    """多跳中间候选"""
    return x * 2


def new_api2(x):
    """多跳链终点"""
    return x * 2


class Service:
    def __init__(self, name):
        self.name = name

    def process(self, x):
        return x * 2
''',
        "_impl.py": '''\
def cross_new_api(x):
    """跨模块 import 转发目标"""
    return x * 2


def dotted_target(x):
    """点链调用目标"""
    return x * 2
''',
    }),
    # v1.1.0：old_api 转发 new_api；aliased 变赋值别名；simple_api 删除；
    #         chain_api 转发 mid_api（多跳第一步）；新增跨模块 / 函数内 import /
    #         method self / 库外 等形态。
    ("v1.1.0", {
        "mod.py": '''\
from . import _impl
from ._impl import cross_new_api


def old_api(x):
    """转发到 new_api"""
    return new_api(x)


aliased = new_api()

arg_alias = new_api(x, y)

chain_alias = old_api()

cross_alias = cross_new_api()

external_alias = np.array

cycle_a = cycle_b()
cycle_b = cycle_a()


def nested_api(x):
    """嵌套调用（非纯转发）"""
    y = _helper(x)
    return y + 1


def _helper(x):
    return x * 2


def new_api(x):
    """全程存在"""
    return x * 2


def chain_api(x):
    """多跳第一步：转发到 mid_api"""
    return mid_api(x)


def mid_api(x):
    """多跳中间候选（本版本仍为定义）"""
    return x * 2


def new_api2(x):
    """多跳链终点"""
    return x * 2


def cross_api(x):
    """跨模块 import 转发"""
    return cross_new_api(x)


def dotted_api(x):
    """点链调用跨模块目标"""
    return _impl.dotted_target(x)


def fqn_forward(x):
    """完整 FQN 调用（绝对导入，无需 import）"""
    return fakelib.mod.new_api(x)


def local_import_api(x):
    """函数内局部 import 后调用"""
    from ._impl import cross_new_api
    return cross_new_api(x)


def external_api(x):
    """嵌套调用库外 API（np 未 import）"""
    y = np.where(x)
    return y


class CrossClass:
    def run(self, x):
        return self._impl_method(x)

    def _impl_method(self, x):
        return x * 2


class Service:
    def __init__(self, name):
        self.name = name

    def process(self, x):
        return x * 2
''',
        "_impl.py": '''\
def cross_new_api(x):
    """跨模块 import 转发目标"""
    return x * 2


def dotted_target(x):
    """点链调用目标"""
    return x * 2
''',
    }),
    # v2.0.0：new_api 保留；mid_api 转发到 new_api2（多跳第二步）；跨模块/类移除
    ("v2.0.0", {
        "mod.py": '''\
def new_api(x):
    """全程存在"""
    return x * 2


def mid_api(x):
    """多跳第二步：转发到 new_api2"""
    return new_api2(x)


def new_api2(x):
    """多跳链终点"""
    return x * 2
''',
        "_impl.py": '''\
# 跨模块 API 已在 v2.0.0 移除
''',
    }),
    # v3.0.0：多跳终点，仅 new_api / new_api2 保留
    ("v3.0.0", {
        "mod.py": '''\
def new_api(x):
    """全程存在"""
    return x * 2


def new_api2(x):
    """多跳链终点"""
    return x * 2
''',
        "_impl.py": '''\
# 跨模块 API 已移除
''',
    }),
]


def _git(repo: str, *args: str) -> None:
    """在 repo 内执行 git 命令。

    输入参数：
        repo (str)：仓库路径。
        *args (str)：git 子命令及参数。
    异常：
        subprocess.CalledProcessError：git 命令失败时抛出。
    """
    subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True)


def make_fakelib(out_dir: str) -> str:
    """构造假库 git 仓库。

    输入参数：
        out_dir (str)：仓库输出目录（已存在则清空重建）。
    返回值：
        str：仓库根目录。
    异常：
        OSError：文件创建失败。
        subprocess.CalledProcessError：git 操作失败。
    """
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    _git(out_dir, 'init', '-q')
    _git(out_dir, 'config', 'user.email', 'test@example.com')
    _git(out_dir, 'config', 'user.name', 'test')

    pkg = os.path.join(out_dir, 'fakelib')
    os.makedirs(pkg)
    with open(os.path.join(pkg, '__init__.py'), 'w', encoding='utf-8') as f:
        f.write('')  # 空 __init__：不产生 __init__ 重导出的公开名，聚焦 internal fqn

    for tag, files in _VERSIONS:
        for rel, src in files.items():
            with open(os.path.join(pkg, rel), 'w', encoding='utf-8') as f:
                f.write(src)
        _git(out_dir, 'add', '-A')
        _git(out_dir, 'commit', '-q', '-m', tag)
        _git(out_dir, 'tag', tag)
    return out_dir


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='构造 PEAR 测试假库 git 仓库')
    parser.add_argument('--out', required=True, help='仓库输出目录')
    args = parser.parse_args()
    repo = make_fakelib(args.out)
    print(f"fake_lib 已生成: {repo}")
