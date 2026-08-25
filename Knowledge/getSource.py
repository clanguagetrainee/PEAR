## @package getSource
#  各版本源码获取
#  职责：通过 git worktree 将指定版本（tag）从 libRepoPath 检出到独立目录，
#  供 extractApi 读取该版本源码；提取完成后负责移除 worktree，不污染主仓库。
#  对应设计文档 §4「git worktree add <tmp> <tag>，提取完成即 git worktree remove」。

import os
import shutil
import subprocess
from typing import Optional


## 默认 worktree 根目录（系统临时目录下）
_DEFAULT_WORKTREES_ROOT = '/tmp/pear_worktrees'


def _git(repo_path: str, *args: str) -> str:
    """在 repo_path 仓库内执行 git 命令并返回 stdout。

    输入参数：
        repo_path (str)：git 仓库路径。
        *args (str)：git 子命令及其参数。
    返回值：
        str：git 命令的标准输出（去除尾部换行）。
    异常：
        subprocess.CalledProcessError：git 命令失败时抛出。
    """
    proc = subprocess.run(
        ['git', '-C', repo_path, *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.rstrip('\n')


def worktree_path(lib_name: str, version: str, root: Optional[str] = None) -> str:
    """计算某版本 worktree 的目标目录路径。

    输入参数：
        lib_name (str)：库名，用于目录命名。
        version (str)：规范化版本号。
        root (Optional[str])：worktree 根目录；默认 /tmp/pear_worktrees。
    返回值：
        str：worktree 目标目录绝对路径。
    """
    base = root or _DEFAULT_WORKTREES_ROOT
    return os.path.join(base, f"{lib_name}_{version}")


def _cleanup_residue(repo_path: str, dest: str) -> None:
    """清理指定目录的残留（上一次运行中断可能遗留已注册的 worktree 或孤儿目录）。

    输入参数：
        repo_path (str)：git 仓库路径。
        dest (str)：残留目录路径。
    异常：
        OSError：目录存在但既不是 worktree 也无法删除时抛出。
    """
    if not os.path.exists(dest):
        return
    proc = subprocess.run(
        ['git', '-C', repo_path, 'worktree', 'remove', '--force', dest],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 and os.path.exists(dest):
        shutil.rmtree(dest)


def checkout_version(repo_path: str, tag: str, dest: str) -> str:
    """将仓库指定 tag 检出到 worktree 目录。

    输入参数：
        repo_path (str)：git 仓库路径。
        tag (str)：要检出的原始 tag 名称。
        dest (str)：worktree 目标目录（若存在残留则先清理）。
    返回值：
        str：检出后的源码目录路径（即 dest）。
    异常：
        subprocess.CalledProcessError：git worktree add 失败时抛出
            （例如 tag 不存在）。
    """
    _cleanup_residue(repo_path, dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _git(repo_path, 'worktree', 'add', dest, tag)
    return dest


def remove_worktree(repo_path: str, dest: str) -> None:
    """移除指定 worktree 目录并注销，释放磁盘与 git 元数据。

    输入参数：
        repo_path (str)：git 仓库路径。
        dest (str)：worktree 目录路径。
    异常：
        subprocess.CalledProcessError：移除失败时抛出。
    """
    if os.path.isdir(dest):
        _git(repo_path, 'worktree', 'remove', '--force', dest)
