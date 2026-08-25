## @package pipeline
#  迭代编排与最终推荐汇总
#  职责：唯一的**有状态编排层**，持有 visited 集合与 SourceProvider 生命周期，
#  BFS 组合 Adjust.get_boundary / Adjust.resolve_api / Recommend.recommend 原语，
#  沿演化足迹迭代展开候选分支；最终合并去重、路径评分（∏ 各跳相似度）、排序，
#  输出目标版本中的 replacement recommendation 候选列表。对应设计文档 §8 流程。

from collections import deque
from typing import List, Tuple

from Tool.model import Candidate, KnowledgeBase, Result, Task
from Tool.tool import SourceProvider
from Adjust.getBoundary import get_boundary
from Adjust.resolveApi import resolve_api
from Recommend.recommend import recommend


def _final_score(c: Candidate) -> float:
    """路径评分：各跳相似度连乘。

    输入参数：
        c (Candidate)：候选（local_scores 为路径上每跳相似度）。
    返回值：
        float：连乘评分；空路径返回 0.0。
    """
    score = 1.0
    for s in c.local_scores:
        score *= s
    return score


def run_pipeline(task: Task, kb: KnowledgeBase, cache_dir: str = 'CodeCache') -> Result:
    """BFS 迭代展开演化链并汇总最终推荐。

    输入参数：
        task (Task)：分析任务（Vs/Vt/old_api_fqn/top_k/lib_repo_path）。
        kb (KnowledgeBase)：内存知识库。
        cache_dir (str)：完整定义缓存根目录，默认 'CodeCache'。
    返回值：
        Result：
          - status='OK'：candidates 为去重后按最终评分降序的候选列表；
          - 'NOT_DEPRECATED'：old_api_fqn 在 [Vs,Vt] 全程存在（未弃用），candidates 空；
          - 'NOT_FOUND'：old_api_fqn 在 Vs 即不存在，candidates 空；
          - 'NO_CANDIDATE'：检索始终为空，candidates 空；
          - 'ERROR'：error 字段描述异常（如 worktree 失败）。
    """
    provider = SourceProvider(task.lib_name, task.lib_repo_path, cache_dir)
    try:
        versions = kb.versions
        if task.source_version not in versions or task.target_version not in versions:
            raise ValueError(
                f"Vs/Vt 不在知识库版本序列中: {task.source_version} / {task.target_version}")

        # 截取 [Vs, Vt] 区间（get_boundary 在其上做 index 查找）
        lo = versions.index(task.source_version)
        hi = versions.index(task.target_version)
        window = versions[lo: hi + 1]

        # queue 元素：(fqn, pos, path, scores)
        queue = deque([(task.old_api_fqn, task.source_version, [], [])])
        visited = set()
        final: List[Candidate] = []

        while queue:
            fqn, pos, path, scores = queue.popleft()
            if fqn in visited:
                continue
            visited.add(fqn)

            boundary = get_boundary(fqn, pos, task.target_version, kb, window)
            if boundary.status == 'NOT_DEPRECATED':
                if fqn == task.old_api_fqn:
                    return Result(task=task, status='NOT_DEPRECATED')
                # 迭代分支在 Vt 仍存在：已在候选 ∈ Vt 检查处作为有效候选记录
                continue
            if boundary.status == 'NOT_FOUND':
                if fqn == task.old_api_fqn:
                    return Result(task=task, status='NOT_FOUND')
                # 迭代分支候选在 vpost 必存在（来自 vpost 候选全集），NOT_FOUND 理论不发生
                continue

            resolved = resolve_api(fqn, boundary.vpre, task.api_type, kb, provider)
            if resolved.kind == 'alias_external':
                continue  # 别名指向外部 API -> 空推荐死分支

            cands = recommend(resolved, boundary.vpre, boundary.vpost,
                              task.api_type, kb, provider, task.top_k)
            if not cands:
                continue  # 检索为空 -> 死分支

            for c in cands:
                new_path = path + [c.fqn]
                new_scores = scores + [c.similarity]
                if kb.exists(c.fqn, task.target_version):
                    final.append(Candidate(
                        fqn=c.fqn, api_type=c.api_type, similarity=0.0,
                        evolution_path=new_path, local_scores=new_scores))
                else:
                    queue.append((c.fqn, boundary.vpost, new_path, new_scores))

        if not final:
            return Result(task=task, status='NO_CANDIDATE')

        # 汇总：按 fqn 合并去重，保留路径评分最高者
        merged: dict[str, Candidate] = {}
        for c in final:
            score = _final_score(c)
            if c.fqn not in merged or score > _final_score(merged[c.fqn]):
                c.similarity = score  # 写入 final_score
                merged[c.fqn] = c
        ranked = sorted(merged.values(), key=_final_score, reverse=True)[: task.top_k]
        return Result(task=task, status='OK', candidates=ranked)

    except Exception as e:
        print(f"[pipeline] 分析失败: {type(e).__name__}: {e}")
        return Result(task=task, status='ERROR', error=f"{type(e).__name__}: {e}")
    finally:
        provider.close()
