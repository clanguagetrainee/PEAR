# -*- coding: utf-8 -*-
"""实验脚本（并行版）：对 cases.xlsx 逐行调用 PEAR 与 Baseline，输出推荐列表与 Top-k 命中统计。

并行策略：
  - 使用 multiprocessing fork 启动，父进程一次性加载三个库的 KnowledgeBase，
    子进程通过 fork COW 继承（只读共享，几乎不占额外内存）。
  - 行级并行跑实验。每个 worker 进程使用自己的 worktree 根目录
    （/tmp/pear_worktrees_p{PID}），同一 process 内跨行复用 SourceProvider
    （worktree 只 checkout 一次，AST 缓存命中原样复用），Provider 通过
    run_pipeline(provider=...) 传入，避免每行重建。
  - 结果由主进程汇总写入 experiment_results_top{top_k}.xlsx。
"""

import argparse
import atexit
import json
import multiprocessing as mp
import os
import sys
import time

import pandas as pd

PEAR_ROOT = "/home/he/PEAR"
sys.path.insert(0, PEAR_ROOT)

from Tool.model import Task                                    # noqa: E402
from Tool.tool import SourceProvider, load_knowledge_base      # noqa: E402
from Pipeline.pipeline import run_pipeline                     # noqa: E402
from Baseline.direct_baseline import direct_baseline           # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CASES_XLSX = os.path.join(PEAR_ROOT, "EvalData", "cases.xlsx")
KB_DIR = os.path.join(PEAR_ROOT, "LibAPIExtraction")
LIBRARIES_ROOT = os.path.join(PEAR_ROOT, "Libraries")
CACHE_DIR = os.path.join(PEAR_ROOT, "CodeCache")

TARGET_VERSION = {"pandas": "3.0.5", "matplotlib": "3.11.1", "django": "6.0.7"}

COL_VERSION = 1       # B
COL_OLD_FQN = 2       # C
COL_API_TYPE = 7      # H：粒度（中文）
COL_REPLACEMENT = 17  # R：真实替代 API FQN

API_TYPE_MAP = {"函数": "function", "方法": "method", "类": "class"}

# ---------------------------------------------------------------------------
# 进程级全局状态（fork 后每个 worker 独立一份）
# ---------------------------------------------------------------------------
_KBS = {}               # lib -> KnowledgeBase（fork COW 继承，只读共享）
_WORKER_PROVIDERS = {}  # lib -> SourceProvider（per-process 懒创建，跨行复用）
_TOP_K = 20                  # main() 按 --top-k 覆盖，fork 后 worker 继承
_K_LIST = [1, 3, 5, 10, 20]  # main() 按 _TOP_K 动态生成（统计 hit@k 用）


def _get_worker_provider(lib_name):
    """当前进程（worker）为 lib 创建/复用的 SourceProvider。

    每个 worker 用唯一 worktree 根（/tmp/pear_worktrees_p{PID}），避免跨进程
    worktree 冲突；同一 process 内跨行共享 provider（worktree 只 checkout 一次，
    AST 解析缓存复用）。进程退出时 atexit 自动关闭 provider 清理 worktree。
    """
    if lib_name not in _WORKER_PROVIDERS:
        repo_path = os.path.join(LIBRARIES_ROOT, lib_name)
        provider = SourceProvider(
            lib_name, repo_path, CACHE_DIR,
            worktrees_root=f"/tmp/pear_worktrees_p{os.getpid()}")
        atexit.register(provider.close)
        _WORKER_PROVIDERS[lib_name] = provider
    return _WORKER_PROVIDERS[lib_name]


def hit_rank(ranked_list, target):
    """返回 target 在 ranked_list 中的 1-based 排名；不在则返回 -1。"""
    if target is None:
        return -1
    try:
        return ranked_list.index(target) + 1
    except ValueError:
        return -1


# ---------------------------------------------------------------------------
# 行级并行实验
# ---------------------------------------------------------------------------

def _run_row(task_arg):
    """worker 执行单行实验：PEAR（run_pipeline + 复用 provider）+ Baseline。"""
    lib_name = task_arg["lib"]
    old_fqn = task_arg["old_api_fqn"]
    source_version = task_arg["source_version"]
    api_type = task_arg["api_type"]       # 已在父进程转换为英文
    replacement = task_arg["replacement"]
    target_version = TARGET_VERSION[lib_name]
    kb = _KBS[lib_name]
    provider = _get_worker_provider(lib_name)

    task = Task(
        lib_name=lib_name, source_version=source_version,
        target_version=target_version, old_api_fqn=old_fqn,
        api_type=api_type, top_k=_TOP_K,
        lib_repo_path=os.path.join(LIBRARIES_ROOT, lib_name),
    )

    # ---- PEAR ----
    pear_status = "ERROR"; pear_list = []; pear_err = None
    pear_result = None
    t0 = time.time()
    try:
        result = run_pipeline(task, kb, cache_dir=CACHE_DIR, provider=provider)
        pear_status = result.status
        pear_list = [c.fqn for c in result.candidates]
        pear_result = result.to_dict()
        if result.status == "ERROR":
            pear_err = result.error
    except Exception as e:
        pear_status = "EXCEPTION"; pear_err = f"{type(e).__name__}: {e}"
    pear_sec = time.time() - t0

    # run_pipeline 自身抛出（未返回 Result）时，兜底一个最小结果结构
    if pear_result is None:
        pear_result = {
            "task": task.to_dict(),
            "status": pear_status,
            "error": pear_err,
            "candidates": [],
            "trace": [],
        }

    # ---- Baseline ----
    base_candidates = []; base_err = None
    t0 = time.time()
    try:
        base_candidates = direct_baseline(
            old_fqn, source_version, target_version, api_type, provider, _TOP_K)
    except Exception as e:
        base_err = f"{type(e).__name__}: {e}"
    base_sec = time.time() - t0
    base_list = [c.fqn for c in base_candidates]

    pear_rank = hit_rank(pear_list, replacement)
    base_rank = hit_rank(base_list, replacement)

    out = {
        "lib_name": lib_name,
        "source_version": source_version,
        "target_version": target_version,
        "old_api_fqn": old_fqn,
        "api_type": api_type,
        "real_replacement": replacement,
        "pear_result": pear_result,
        "baseline_candidates": base_candidates,
        "baseline_error": base_err,
    }
    for k in _K_LIST:
        out[f"pear_hit@{k}"] = int(0 < pear_rank <= k)
        out[f"baseline_hit@{k}"] = int(0 < base_rank <= k)

    print(f"[{lib_name}] {old_fqn} | "
          f"PEAR:{pear_status}({len(pear_list)}个,rank={pear_rank},{pear_sec:.1f}s) | "
          f"Baseline:({len(base_list)}个,rank={base_rank},{base_sec:.1f}s)",
          flush=True)
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    mp.set_start_method("fork")

    parser = argparse.ArgumentParser(description="PEAR/Baseline 实验（并行版）")
    parser.add_argument("--limit", type=int, default=0,
                        help="每个 lib 只跑前 N 行（0=全部），用于冒烟测试")
    parser.add_argument("--libs", type=str, default="",
                        help="逗号分隔的 lib 过滤，空=全部")
    parser.add_argument("--workers", type=int, default=0,
                        help="并行 worker 数（默认 min(32, cpu_count)）")
    parser.add_argument("--top-k", type=int, default=20,
                        help="Top-k 推荐数量（默认 20）")
    args = parser.parse_args()

    libs_filter = [s.strip() for s in args.libs.split(",") if s.strip()]
    cpu = os.cpu_count() or 1
    n_workers = args.workers or max(1, min(32, cpu))

    global _TOP_K, _K_LIST
    _TOP_K = args.top_k
    _K_LIST = sorted(k for k in {1, 3, 5, 10, 20, _TOP_K} if k <= _TOP_K)

    # 读取 sheet，构造任务列表
    xl = pd.ExcelFile(CASES_XLSX)
    tasks = []  # list of dict (plain, picklable)
    for sheet in xl.sheet_names:
        if libs_filter and sheet not in libs_filter:
            continue
        df = xl.parse(sheet)
        if args.limit > 0:
            df = df.head(args.limit)
        for _, row in df.iterrows():
            old_fqn = str(row.iloc[COL_OLD_FQN]).strip()
            sv = str(row.iloc[COL_VERSION]).strip()
            api_cn = str(row.iloc[COL_API_TYPE]).strip()
            api_type = API_TYPE_MAP.get(api_cn, api_cn)
            repl = row.iloc[COL_REPLACEMENT]
            if pd.isna(repl):
                repl = None
            else:
                repl = str(repl).strip()
            tasks.append({
                "lib": sheet,
                "old_api_fqn": old_fqn,
                "source_version": sv,
                "api_type": api_type,
                "replacement": repl,
            })

    selected_libs = sorted({t["lib"] for t in tasks})
    print(f"库: {selected_libs}, 总行数: {len(tasks)}, workers: {n_workers}, top_k: {_TOP_K}")

    # 父进程加载 KB（fork 后子进程 COW 继承，只读共享）
    global _KBS
    for lib in selected_libs:
        _KBS[lib] = load_knowledge_base(lib, KB_DIR)

    # 行级并行实验
    print(f"\n开始行级并行实验（{len(tasks)} 行）...")
    t_start = time.time()
    with mp.Pool(processes=n_workers) as pool:
        results = pool.map(_run_row, tasks, chunksize=1)
    elapsed = time.time() - t_start
    print(f"\n完成，耗时 {elapsed:.0f}s ({elapsed/60:.1f}min)")

    # ---- 汇总统计（数值，不百分比）----
    all_detail = {}  # lib -> list of row dicts
    for row in results:
        all_detail.setdefault(row["lib_name"], []).append(row)

    summary_stats = {}  # lib -> {"n_rows": int, "pear_hit@k": int, "baseline_hit@k": int}
    for lib in selected_libs:
        rows = all_detail.get(lib, [])
        stat = {"n_rows": len(rows)}
        for k in _K_LIST:
            stat[f"pear_hit@{k}"] = sum(row[f"pear_hit@{k}"] for row in rows)
            stat[f"baseline_hit@{k}"] = sum(row[f"baseline_hit@{k}"] for row in rows)
        summary_stats[lib] = stat

    total_n = sum(stat["n_rows"] for stat in summary_stats.values())
    total_stat = {"n_rows": total_n}
    for k in _K_LIST:
        total_stat[f"pear_hit@{k}"] = sum(row[f"pear_hit@{k}"] for row in results)
        total_stat[f"baseline_hit@{k}"] = sum(row[f"baseline_hit@{k}"] for row in results)
    summary_stats["TOTAL"] = total_stat

    # ---- 写 PEAR / Baseline 目录（一个推荐任务一个文件，原汁原味）----
    pear_dir = os.path.join(PEAR_ROOT, "EvalData", "PEAR")
    baseline_dir = os.path.join(PEAR_ROOT, "EvalData", "Baseline")
    os.makedirs(pear_dir, exist_ok=True)
    os.makedirs(baseline_dir, exist_ok=True)
    for i, row in enumerate(results):
        # PEAR：直接落 Result.to_dict()（task/status/error/candidates/trace）
        with open(os.path.join(pear_dir, f"{i:04d}_{row['lib_name']}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(row["pear_result"], f, ensure_ascii=False, indent=2)
        # Baseline：task + candidates（task 复用 PEAR 的 task；出错时附带 error）
        base_record = {
            "task": row["pear_result"]["task"],
            "candidates": [c.to_dict() for c in row["baseline_candidates"]],
        }
        if row["baseline_error"] is not None:
            base_record["error"] = row["baseline_error"]
        with open(os.path.join(baseline_dir, f"{i:04d}_{row['lib_name']}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(base_record, f, ensure_ascii=False, indent=2)

    # ---- 写汇总 json（每实验一条 + 各库汇总统计，数值不含百分比）----
    records = []
    for row in results:
        rec = {
            "lib_name": row["lib_name"],
            "source_version": row["source_version"],
            "target_version": row["target_version"],
            "old_api_fqn": row["old_api_fqn"],
            "api_type": row["api_type"],
            "real_replacement": row["real_replacement"],
        }
        for k in _K_LIST:
            rec[f"pear_hit@{k}"] = row[f"pear_hit@{k}"]
            rec[f"baseline_hit@{k}"] = row[f"baseline_hit@{k}"]
        records.append(rec)
    summary_json = os.path.join(PEAR_ROOT, "EvalData",
                                f"experiment_summary_top{_TOP_K}.json")
    with open(summary_json, "w", encoding="utf-8") as f:
        json.dump({"records": records, "summary": summary_stats},
                  f, ensure_ascii=False, indent=2)

    # ---- 控制台汇总（数值）----
    print("\n===== 汇总 =====")
    for lib in selected_libs + ["TOTAL"]:
        s = summary_stats[lib]
        cells = [f"@{k}:{s[f'pear_hit@{k}']}/{s[f'baseline_hit@{k}']}" for k in _K_LIST]
        print(f"{lib:12s} n={s['n_rows']:3d}  " + "  ".join(cells))
    print(f"\nPEAR 结果目录: {pear_dir}/")
    print(f"Baseline 结果目录: {baseline_dir}/")
    print(f"汇总 json: {summary_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())