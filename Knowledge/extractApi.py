## @package extractApi
#  API 提取接口（薄包装）
#  职责：调用 vendored 的 _pcart.getDefFunction 完成单个版本的库 API 提取，
#  输出与现有 LibAPIExtraction 文件一致的 PCART 文本格式 {libName}{version}。
#  提取逻辑本身复用 PCART（见 _pcart/README.md），本文件仅做参数化封装：
#  传入版本源码根目录与输出目录，不重新实现任何 AST 提取。

import os
from typing import Optional

from Knowledge._pcart.getDef import getDefFunction  # vendored PCART 提取


def extract_lib_api(lib_name: str, version: str, lib_path: str,
                    out_dir: str = 'LibAPIExtraction',
                    package_root: Optional[str] = None) -> str:
    """提取单个版本的库 API，输出 PCART 文本格式文件。

    输入参数：
        lib_name (str)：库名。
        version (str)：规范化版本号。
        lib_path (str)：该版本源码根目录（git worktree 检出目录）。
        out_dir (str)：知识库输出根目录，默认 'LibAPIExtraction'。
        package_root (Optional[str])：库包目录（遍历范围），
            如 repo/pandas 或 repo/lib/matplotlib；None 则用 lib_path。
    返回值：
        str：输出文件路径 {out_dir}/{lib_name}/{lib_name}{version}。
    异常：
        OSError：输出目录创建或文件写入失败时抛出。
    """
    getDefFunction((lib_name, version, lib_path), out_dir=out_dir,
                   package_root=package_root)
    return os.path.join(out_dir, lib_name, f"{lib_name}{version}")
