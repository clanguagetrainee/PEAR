## @package build
#  弃用知识库构建入口
#  职责：串联 getVersion（git tag 版本序列）→ getSource（worktree 切版本）→
#  extractApi（复用 PCART 提取）→ 落盘。已有版本跳过不重提，仅补充缺失版本。
#  对应设计文档 §3 阶段①、§8 处理流程 [阶段①] Knowledge.build。

import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from Knowledge.getVersion import list_versions
from Knowledge.getSource import checkout_version, remove_worktree, worktree_path
from Knowledge.extractApi import extract_lib_api


def _version_key(version: str):
    """版本号字符串 → 可比较数字元组。"""
    return tuple(int(part) for part in version.split('.'))


def _within(version: str, lo: Optional[str], hi: Optional[str]) -> bool:
    """判断版本是否落在 [lo, hi] 区间（含端点），None 表示不限。"""
    key = _version_key(version)
    if lo is not None and key < _version_key(lo):
        return False
    if hi is not None and key > _version_key(hi):
        return False
    return True


def _resolve_package_root(lib_path: str, lib_name: str) -> str:
    """探测库包目录（提取遍历范围）。

    优先同名包目录（pandas/django 直接位于仓库根下），
    其次 lib/{lib_name} 布局（matplotlib 为 lib/matplotlib），
    否则退回源码根。
    """
    for cand in (os.path.join(lib_path, lib_name),
                 os.path.join(lib_path, 'lib', lib_name)):
        if os.path.isdir(cand):
            return cand
    return lib_path


def _already_extracted(lib_name: str, version: str, out_dir: str) -> bool:
    """判定该版本知识库文件是否已存在。

    兼容两种命名：PCART 文本格式 {libName}{version}（现有文件）与
    后续 json 格式 {version}.json。
    """
    base = os.path.join(out_dir, lib_name)
    return (os.path.exists(os.path.join(base, f"{lib_name}{version}"))
            or os.path.exists(os.path.join(base, f"{version}.json")))


def _process_one_version(lib_name: str, repo_path: str, version: str, tag: str,
                         out_dir: str,
                         worktrees_root: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """提取单个版本：checkout worktree → 提取 API → 清理 worktree。

    供 build_knowledge 串行/并行（ProcessPoolExecutor）复用。并行下 git
    worktree add 偶发锁竞争，checkout 失败自动重试 3 次（线性退避）。

    输入参数：
        lib_name (str)：库名。
        repo_path (str)：git 仓库路径。
        version (str)：规范化版本号。
        tag (str)：原始 git tag。
        out_dir (str)：知识库输出根目录。
        worktrees_root (Optional[str])：worktree 根目录，None 用默认。
    返回值：
        Tuple[str, str, Optional[str]]：(version, tag, error)；
            成功时 error 为 None，失败时为错误描述字符串。
    """
    dest = worktree_path(lib_name, version, worktrees_root)
    lib_path = None
    last_err: Optional[Exception] = None
    for attempt in range(3):  # git worktree 锁竞争偶发，失败重试
        try:
            lib_path = checkout_version(repo_path, tag, dest)
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    try:
        if lib_path is None:
            return version, tag, f"worktree checkout 重试 3 次失败: {last_err}"
        pkg_root = _resolve_package_root(lib_path, lib_name)
        extract_lib_api(lib_name, version, lib_path, out_dir=out_dir,
                        package_root=pkg_root)
        return version, tag, None
    except Exception as e:
        return version, tag, str(e)
    finally:
        try:
            if os.path.isdir(dest):
                remove_worktree(repo_path, dest)
        except Exception as e:
            print(f"[build] {lib_name} {version} worktree 清理失败: {e}")


def build_knowledge(lib_name: str, repo_path: str, out_dir: str = 'LibAPIExtraction',
                    source_version: Optional[str] = None,
                    target_version: Optional[str] = None,
                    worktrees_root: Optional[str] = None,
                    jobs: int = 1) -> Dict:
    """构建（补齐）指定库的弃用知识库。

    输入参数：
        lib_name (str)：库名。
        repo_path (str)：本地 git 仓库路径（libRepoPath）。
        out_dir (str)：知识库输出根目录，默认 'LibAPIExtraction'。
        source_version (Optional[str])：版本区间下界（含），None 表示不限。
        target_version (Optional[str])：版本区间上界（含），None 表示不限。
        worktrees_root (Optional[str])：worktree 根目录，默认 /tmp/pear_worktrees。
        jobs (int)：并行进程数，>1 时用 ProcessPoolExecutor 并行提取（默认 1 串行）。
    返回值：
        Dict：统计信息 {total, existing, extracted, failed, output_dir}，
            其中 failed 为 [{"version", "tag", "error"}, ...] 失败版本列表。
    异常：
        FileNotFoundError：仓库路径不存在时抛出。
    """
    versions = list_versions(repo_path)
    selected = [(v, t) for v, t in versions if _within(v, source_version, target_version)]

    todo = [(v, t) for v, t in selected if not _already_extracted(lib_name, v, out_dir)]
    existing = len(selected) - len(todo)
    extracted = 0
    failed: List[Dict] = []

    def _run(v: str, t: str) -> None:
        nonlocal extracted
        _, _, err = _process_one_version(lib_name, repo_path, v, t, out_dir,
                                         worktrees_root)
        if err:
            failed.append({"version": v, "tag": t, "error": err})
            print(f"[build] {lib_name} {v} 提取失败: {err}")
        else:
            extracted += 1
            print(f"[build] {lib_name} {v} 提取完成（新增第 {extracted} 个）")

    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futures = [ex.submit(_process_one_version, lib_name, repo_path, v, t,
                                 out_dir, worktrees_root) for v, t in todo]
            for fut in as_completed(futures):
                v, t, err = fut.result()
                if err:
                    failed.append({"version": v, "tag": t, "error": err})
                    print(f"[build] {lib_name} {v} 提取失败: {err}")
                else:
                    extracted += 1
                    print(f"[build] {lib_name} {v} 提取完成（新增第 {extracted} 个）")
    else:
        for v, t in todo:
            _run(v, t)

    print(f"[build] {lib_name} 构建完成: "
          f"区间内 {len(selected)} 个版本 / 已有 {existing} / 新提取 {extracted} / 失败 {len(failed)}")
    for item in failed:
        print(f"  - 失败 {item['version']} (tag={item['tag']}): {item['error']}")
    return {
        "total": len(selected),
        "existing": existing,
        "extracted": extracted,
        "failed": failed,
        "output_dir": os.path.abspath(out_dir),
    }


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="PEAR 弃用知识库构建")
    parser.add_argument('--lib', required=True, help='库名')
    parser.add_argument('--repo', required=True, help='本地 git 仓库路径')
    parser.add_argument('--out', default='LibAPIExtraction', help='知识库输出目录')
    parser.add_argument('--source', default=None, help='版本区间下界（含）')
    parser.add_argument('--target', default=None, help='版本区间上界（含）')
    parser.add_argument('--jobs', type=int, default=1, help='并行进程数（默认 1 串行）')
    args = parser.parse_args()

    result = build_knowledge(args.lib, args.repo, out_dir=args.out,
                             source_version=args.source, target_version=args.target,
                             jobs=args.jobs)
    if result['failed']:
        raise SystemExit(f"存在 {len(result['failed'])} 个版本提取失败，详见上方输出")
