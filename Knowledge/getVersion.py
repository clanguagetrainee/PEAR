## @package getVersion
#  版本序列获取
#  职责：从本地 git 仓库（libRepoPath）读取 tag，规范化版本号（去除 v 前缀、
#  过滤 rc/beta/dev/post 等预发布与构建版本），按语义版本号排序，得到按演化
#  顺序排列的稳定版本序列，供 build 阶段逐版本提取 API 使用。
#  PCART 无此能力（其只处理 config 写死的两个版本），为本项目新增的
#  「遍历切换版本」底座之一。对应设计文档 §4「源码获取与版本号」。

import os
import re
import subprocess
from typing import List, Optional, Tuple


## 纯版本号 tag 正则
#  仅匹配形如 2.0 / 2.0.0 / v2.0.0 的 tag，天然排除 rc/beta/dev/post、
#  debian/ 前缀、stable/ 分支 tag 等非标准发布 tag。
_PURE_TAG = re.compile(r'^v?(\d+\.\d+(?:\.\d+)*)$')


def normalize_tag(tag: str) -> Optional[str]:
    """将单个 git tag 规范化为纯版本号字符串。

    输入参数：
        tag (str)：git tag 名称，如 "v2.0.0"、"2.0"、"debian/0.4.0-1"。
    返回值：
        Optional[str]：规范化后的纯数字版本号（如 "2.0.0"）；
            无法规范化（预发布、分支 tag 等）时返回 None。
    """
    m = _PURE_TAG.fullmatch(tag.strip())
    return m.group(1) if m else None


def _version_key(version: str) -> Tuple[int, ...]:
    """将版本号字符串转成可排序的数字元组，用于语义版本比较。

    输入参数：
        version (str)：纯数字版本号，如 "2.0.0"。
    返回值：
        Tuple[int, ...]：各数字段组成的元组。
    """
    return tuple(int(part) for part in version.split('.'))


def list_versions(repo_path: str) -> List[Tuple[str, str]]:
    """列出仓库中按演化顺序排列的稳定版本 (version, tag) 序列。

    输入参数：
        repo_path (str)：本地 git 仓库路径（libRepoPath）。
    返回值：
        List[Tuple[str, str]]：按版本号升序排列的 (规范化版本号, 原始 tag) 列表。
    异常：
        FileNotFoundError：repo_path 不是有效目录时抛出。
        subprocess.CalledProcessError：git tag 执行失败时抛出。
    """
    if not os.path.isdir(repo_path):
        raise FileNotFoundError(f"仓库路径不存在: {repo_path}")
    proc = subprocess.run(
        ['git', '-C', repo_path, 'tag', '--list'],
        capture_output=True, text=True, check=True,
    )
    pairs: List[Tuple[str, str]] = []
    for tag in proc.stdout.splitlines():
        version = normalize_tag(tag)
        if version is not None:
            pairs.append((version, tag))
    pairs.sort(key=lambda vt: _version_key(vt[0]))
    return pairs
