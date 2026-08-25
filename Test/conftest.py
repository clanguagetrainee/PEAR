## @file conftest.py
#  pytest 测试配置与共享夹具
#  职责：把 PEAR 根目录与 fake_libs 脚本目录加入 sys.path，供各测试 import
#  项目模块；提供两类夹具——
#  1) session 级 fake_repo / fake_kb：构造多版本假库 git 仓库并构建知识库，
#     供 SourceProvider / Pipeline 集成测试复用；
#  2) FakeProvider：纯单元测试用的 SourceProvider 替身（不碰 git / 文件系统）。
#  真库（pandas 等）只作最后端到端验证，不进入本夹具。

import os
import sys

import pytest

# PEAR 根目录加入 sys.path，使 Tool / Adjust / Recommend / Pipeline / Knowledge 可 import
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# fake_libs 脚本目录加入 sys.path，使 make_fakelib 可 import
_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), 'fixtures', 'fake_libs')
if _FIXTURE_DIR not in sys.path:
    sys.path.insert(0, _FIXTURE_DIR)

from make_fakelib import make_fakelib  # noqa: E402
from Knowledge.build import build_knowledge  # noqa: E402
from Tool.tool import load_knowledge_base  # noqa: E402


class FakeProvider:
    """SourceProvider 替身：纯单元测试用，不碰 git / 文件系统。

    用 {fqn: 定义文本} 字典模拟 CodeCache 批次；get_api 返回字典值，
    list_api 返回全部 fqn，ensure_batch / close 为无操作。
    """

    def __init__(self, defs=None):
        """初始化。

        输入参数：
            defs (dict)：{internal_fqn: 完整定义文本} 字典。
        """
        self.defs = dict(defs or {})

    def get_api(self, internal_fqn, api_type, version):
        return self.defs.get(internal_fqn)

    def ensure_batch(self, api_type, version):
        return None

    def list_api(self, api_type, version):
        return sorted(self.defs.keys())

    def close(self):
        return None


@pytest.fixture(scope='session')
def fake_repo(tmp_path_factory):
    """构造多版本假库 git 仓库（session 级，复用）。

    返回值：
        str：假库仓库根目录。
    """
    base = tmp_path_factory.mktemp('fake_repo')
    return make_fakelib(str(base / 'fakelib_git'))


@pytest.fixture(scope='session')
def fake_kb(tmp_path_factory, fake_repo):
    """构建假库知识库并加载为 KnowledgeBase（session 级，复用）。

    输入参数：
        fake_repo (str)：假库仓库路径。
    返回值：
        KnowledgeBase：内存知识库。
    """
    kb_dir = tmp_path_factory.mktemp('fake_kb')
    build_knowledge('fakelib', fake_repo, out_dir=str(kb_dir),
                    source_version='1.0.0', target_version='3.0.0', jobs=1)
    return load_knowledge_base('fakelib', str(kb_dir))


@pytest.fixture()
def provider(fake_repo, tmp_path):
    """构造真实 SourceProvider（function/method/class 通用）。

    依赖 session 级 fake_repo（git 仓库）+ 函数级 tmp_path 缓存目录；测试结束
    调用 close() 清理 worktree。resolveApi / resolveChain 集成测试用它取代
    FakeProvider，走真实 module_ast / locate_module 的 AST 精确解析。

    输入参数：
        fake_repo (str)：假库 git 仓库路径。
        tmp_path (Path)：pytest 临时目录（作 CodeCache）。
    返回值：
        SourceProvider：已就绪（惰性 checkout）的提供者。
    """
    from Tool.tool import SourceProvider
    p = SourceProvider('fakelib', fake_repo, cache_dir=str(tmp_path / 'cache'))
    yield p
    p.close()
