## @file main.py
#  PEAR 主入口
#  职责：解析子命令（build / recommend）与任务配置（Configure JSON + CLI 覆盖），
#  调用 Knowledge.build 构建弃用知识库 / Pipeline.run_pipeline 执行主流程，
#  格式化输出最终 replacement recommendation 候选列表。
#  对应设计文档 §3 两阶段工作流、§8 处理流程的入口编排。

import argparse
import os
import sys
from typing import List, Optional

from Tool.model import Task
from Tool.tool import load_knowledge_base, load_task
from Knowledge.getVersion import list_versions
from Pipeline.pipeline import run_pipeline


## 知识库输出根目录
KB_DIR = 'LibAPIExtraction'


def _version_key(version: str):
    """版本号字符串 → 可比较数字元组。"""
    return tuple(int(part) for part in version.split('.'))


def _selected_versions(repo_path: str, source_version: str, target_version: str) -> List[str]:
    """取 [Vs, Vt] 区间的规范化版本号序列。

    输入参数：
        repo_path (str)：本地 git 仓库路径。
        source_version (str)：Vs。
        target_version (str)：Vt。
    返回值：
        List[str]：区间内升序版本号。
    异常：
        FileNotFoundError：仓库路径不存在。
    """
    pairs = list_versions(repo_path)
    lo = _version_key(source_version)
    hi = _version_key(target_version)
    return [v for v, _ in pairs if lo <= _version_key(v) <= hi]


def _kb_file_path(lib_name: str, version: str, kb_dir: str) -> str:
    """返回某版本知识库文件的预期路径（文本格式）。"""
    return os.path.join(kb_dir, lib_name, f"{lib_name}{version}")


def cmd_build(config_path: str, jobs: int = 1) -> None:
    """读取 Configure JSON → Knowledge.build.build_knowledge 构建知识库。

    输入参数：
        config_path (str)：Configure JSON 文件路径。
        jobs (int)：并行进程数，默认 1 串行。
    """
    from Knowledge.build import build_knowledge
    task = load_task(config_path)
    result = build_knowledge(
        task.lib_name, task.lib_repo_path, out_dir=KB_DIR,
        source_version=task.source_version, target_version=task.target_version,
        jobs=jobs,
    )
    if result['failed']:
        raise SystemExit(f"存在 {len(result['failed'])} 个版本提取失败，详见上方输出")
    print(f"知识库构建完成: {task.lib_name} 区间内 {result['total']} 个版本，"
          f"新提取 {result['extracted']} 个，已有 {result['existing']} 个")


def cmd_recommend(config_path: str, cache_dir: str = 'CodeCache') -> None:
    """读配置 → 检查知识库缺失 → 加载 → run_pipeline → 格式化输出。

    输入参数：
        config_path (str)：Configure JSON 文件路径。
        cache_dir (str)：完整定义缓存根目录，默认 'CodeCache'。
    """
    task = load_task(config_path)

    # 1) 按 [Vs, Vt] 逐版本检查知识库缺失
    versions = _selected_versions(task.lib_repo_path, task.source_version,
                                  task.target_version)
    if not versions:
        raise SystemExit(f"[{task.source_version}, {task.target_version}] 区间内无可用版本")
    missing = [v for v in versions
               if not os.path.isfile(_kb_file_path(task.lib_name, v, KB_DIR))]
    if missing:
        for v in missing:
            print(f"知识库缺失: {_kb_file_path(task.lib_name, v, KB_DIR)}")
        raise SystemExit("请先执行 python main.py build -cfg <path> 构建上述版本")

    # 2) 加载知识库
    kb = load_knowledge_base(task.lib_name, KB_DIR)

    # 3) 主流程
    result = run_pipeline(task, kb, cache_dir=cache_dir)

    # 4) 格式化输出
    _print_result(result)


def _print_result(result) -> None:
    """格式化输出 run_pipeline 结果。

    输入参数：
        result (Result)：Pipeline 返回。
    """
    if result.status == 'OK':
        print(f"\n=== replacement recommendation（{result.task.lib_name} "
              f"{result.task.old_api_fqn}）===")
        for i, c in enumerate(result.candidates, 1):
            print(f"\n#{i}  {c.fqn}  [{c.api_type}]  final_score={c.similarity:.4f}")
            print(f"    evolution_path : {' -> '.join(c.evolution_path)}")
            print(f"    local_scores   : {', '.join(f'{s:.4f}' for s in c.local_scores)}")
        print(f"\n共 {len(result.candidates)} 个候选")
    elif result.status == 'NOT_DEPRECATED':
        print(f"{result.task.old_api_fqn} 在 [{result.task.source_version}, "
              f"{result.task.target_version}] 全程存在，未弃用，无需替代")
    elif result.status == 'NOT_FOUND':
        print(f"{result.task.old_api_fqn} 在 {result.task.source_version} 即不存在")
    elif result.status == 'NO_CANDIDATE':
        print(f"{result.task.old_api_fqn} 未找到有效替代候选")
    elif result.status == 'ERROR':
        print(f"分析失败: {result.error}")
    else:
        print(f"未知状态: {result.status}")


def main(argv: Optional[List[str]] = None) -> None:
    """argparse 入口：{build, recommend} + -cfg <path> [--jobs N]。

    输入参数：
        argv (Optional[List[str]])：命令行参数，None 用 sys.argv[1:]。
    """
    parser = argparse.ArgumentParser(
        prog='PEAR',
        description='Python Evolution-aware API Replacement tool（跨版本弃用 API 替代推荐）')
    sub = parser.add_subparsers(dest='command', required=True)

    p_build = sub.add_parser('build', help='① 构建弃用知识库')
    p_build.add_argument('-cfg', '--config', required=True, help='Configure JSON 路径')
    p_build.add_argument('--jobs', type=int, default=1, help='并行进程数（默认 1）')

    p_rec = sub.add_parser('recommend', help='② 主流程（推荐）')
    p_rec.add_argument('-cfg', '--config', required=True, help='Configure JSON 路径')
    p_rec.add_argument('--cache-dir', default='CodeCache', help='定义缓存根目录')

    args = parser.parse_args(argv)

    if args.command == 'build':
        cmd_build(args.config, jobs=args.jobs)
    elif args.command == 'recommend':
        cmd_recommend(args.config, cache_dir=args.cache_dir)


if __name__ == '__main__':
    main()
