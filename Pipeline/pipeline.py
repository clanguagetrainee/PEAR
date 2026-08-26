## @package pipeline
#  迭代编排与最终推荐汇总
#  职责：唯一的**有状态编排层**，持有 visited 集合与 SourceProvider 生命周期，
#  BFS 组合 Adjust.get_boundary / Adjust.resolve_api / Recommend.recommend 原语，
#  沿演化足迹迭代展开候选分支；最终合并去重、路径评分（∏ 各跳相似度）、排序，
#  输出目标版本中的 replacement recommendation 候选列表。对应设计文档 §8 流程。

import heapq
from typing import List, Optional, Tuple

from Tool.model import Candidate, KnowledgeBase, Result, Task
from Tool.tool import SourceProvider
from Adjust.getBoundary import get_boundary
from Adjust.resolveApi import resolve_api
from Recommend.recommend import recommend


def _path_score(scores: List[float]) -> float:
    """路径分数：各跳相似度连乘（空路径返回 1.0，为最上层哨兵值）。

    输入参数：
        scores (List[float])：路径上每一跳的相似度，值域 [0,1]。
    返回值：
        float：连乘分数，值域 [0,1]；随跳数单调非增（每乘一个 ≤1 的相似度），
            这是 branch-and-bound 剪枝成立的前提。
    """
    score = 1.0
    for s in scores:
        score *= s
    return score


def _final_score(c: Candidate) -> float:
    """候选最终评分：路径上各跳相似度连乘。

    输入参数：
        c (Candidate)：候选（local_scores 为路径上每跳相似度）。
    返回值：
        float：连乘评分；空路径返回 1.0。
    """
    return _path_score(c.local_scores)


def _trace_node(fqn: str, path_score: float, boundary: Optional[str] = None,
                vpre: Optional[str] = None, vpost: Optional[str] = None,
                resolved_fqn: Optional[str] = None,
                resolved_kind: Optional[str] = None,
                candidates: Optional[List[dict]] = None,
                entered_final: Optional[List[str]] = None,
                entered_iter: Optional[List[str]] = None,
                pruned: Optional[List[str]] = None,
                prune_bound: Optional[float] = None,
                dead_reason: Optional[str] = None) -> dict:
    """构造一条 BFS 轨迹节点（dict），随 Result.trace 序列化输出。

    输入参数：
        fqn (str)：当前待分析 API（更正前，即 queue 弹出的 fqn）。
        path_score (float)：到达该节点的路径分数（各跳相似度连乘）。
        boundary (Optional[str])：get_boundary 的状态（DEPRECATED/NOT_DEPRECATED/NOT_FOUND）。
        vpre/vpost (Optional[str])：DEPRECATED 时的失效边界版本对。
        resolved_fqn/resolved_kind (Optional[str])：resolve_api 更正后的实质 API 与 kind。
        candidates (Optional[List[dict]])：该跳推荐前 top_k，元素 {"fqn","similarity"}。
        entered_final/entered_iter/pruned (Optional[List[str]])：候选分类去向。
        prune_bound (Optional[float])：剪枝时的下界（final_heap 堆顶），无剪枝为 None。
        dead_reason (Optional[str])：死分支原因（pruned_at_pop/not_deprecated/
            not_found/alias_external/empty_recommend）。
    返回值：
        dict：一条轨迹记录，字段齐全（缺省 None/空列表），便于 jsonl 统一 schema。
    """
    return {
        "fqn": fqn,
        "path_score": round(path_score, 6),
        "boundary": boundary,
        "vpre": vpre,
        "vpost": vpost,
        "resolved_fqn": resolved_fqn,
        "resolved_kind": resolved_kind,
        "candidates": candidates or [],
        "entered_final": entered_final or [],
        "entered_iter": entered_iter or [],
        "pruned": pruned or [],
        "prune_bound": round(prune_bound, 6) if prune_bound is not None else None,
        "dead_reason": dead_reason,
    }


def run_pipeline(task: Task, kb: KnowledgeBase, cache_dir: str = 'CodeCache',
                 provider: Optional[SourceProvider] = None) -> Result:
    """BFS 迭代展开演化链并汇总最终推荐。

    输入参数：
        task (Task)：分析任务（Vs/Vt/old_api_fqn/top_k/lib_repo_path）。
        kb (KnowledgeBase)：内存知识库。
        cache_dir (str)：完整定义缓存根目录，默认 'CodeCache'。
        provider (Optional[SourceProvider])：外部传入的 SourceProvider（复用场景）。
            None 时内部创建并关闭；非 None 时直接使用，不创建也不关闭。
            worktree 与 AST 缓存可在多次调用间复用。
    返回值：
        Result：
          - status='OK'：candidates 为去重后按最终评分降序的候选列表；
          - 'NOT_DEPRECATED'：old_api_fqn 在 [Vs,Vt] 全程存在（未弃用），candidates 空；
          - 'NOT_FOUND'：old_api_fqn 在 Vs 即不存在，candidates 空；
          - 'NO_CANDIDATE'：检索始终为空，candidates 空；
          - 'ERROR'：error 字段描述异常（如 worktree 失败）。
          - 所有 status 均带 trace：BFS 完整执行轨迹（每跳的调整版本对、更正后
            API、候选及相似度、进 final/进迭代/剪枝/死分支），供诊断与落盘。
    """
    own_provider = provider is None
    if own_provider:
        provider = SourceProvider(task.lib_name, task.lib_repo_path, cache_dir)
    # BFS 完整执行轨迹（每元素一条节点记录），随 Result.trace 返回；放在 try 外
    # 以便 except 分支也能带上已收集的部分轨迹。
    trace: List[dict] = []
    try:
        versions = kb.versions
        if task.source_version not in versions or task.target_version not in versions:
            raise ValueError(
                f"Vs/Vt 不在知识库版本序列中: {task.source_version} / {task.target_version}")

        # 截取 [Vs, Vt] 区间（get_boundary 在其上做 index 查找）
        lo = versions.index(task.source_version)
        hi = versions.index(task.target_version)
        window = versions[lo: hi + 1]

        # 优先队列（小顶堆）：元素 (-路径分数, 自增序号, fqn, pos, path, scores)。
        # 取负使高分数先出，剪枝下界快速收敛、剪得更狠；seq 自增序号保证同分时
        # 不会比较到 list。注意：展开顺序改为按分数降序，不同于原 FIFO，会改变
        # visited 去重时「某 fqn 用哪条路径展开」，故最终 top_k 可能与未优化版不同。
        seq = 0
        queue = [(-1.0, seq, task.old_api_fqn, task.source_version, [], [])]
        visited = set()
        final: List[Candidate] = []
        # 剪枝下界必须且只能来自「已确定存在于 Vt」的候选（final），绝不能用优先
        # 队列里的中间节点——中间节点分数可能很高但不在 Vt，用它做下界会高估而误剪。
        # final_heap 为小顶堆：堆顶 = 当前已收集 final 的第 k 大 = 剪枝下界。
        final_heap: List[float] = []
        # 候选 tokenize 复用缓存：同一 (api_type, vpost) 的全量候选 repr 只构建一次，
        # BFS 沿同一 vpost 反复检索时复用，避免重复 ast.parse + tokenize。
        repr_cache: dict = {}

        while queue:
            neg_score, _seq, fqn, pos, path, scores = heapq.heappop(queue)
            cur_score = -neg_score
            if fqn in visited:
                continue
            visited.add(fqn)
            # 剪枝：当前路径分数已低于下界 ⇒ 整分支进不了 top_k
            if len(final_heap) >= task.top_k and cur_score < final_heap[0]:
                trace.append(_trace_node(
                    fqn=fqn, path_score=cur_score,
                    prune_bound=final_heap[0], dead_reason='pruned_at_pop'))
                continue

            boundary = get_boundary(fqn, pos, task.target_version, kb, window)
            if boundary.status == 'NOT_DEPRECATED':
                if fqn == task.old_api_fqn:
                    trace.append(_trace_node(
                        fqn=fqn, path_score=cur_score, boundary='NOT_DEPRECATED',
                        dead_reason='not_deprecated_root'))
                    return Result(task=task, status='NOT_DEPRECATED', trace=trace)
                # 迭代分支在 Vt 仍存在：已在候选 ∈ Vt 检查处作为有效候选记录
                trace.append(_trace_node(
                    fqn=fqn, path_score=cur_score, boundary='NOT_DEPRECATED',
                    dead_reason='not_deprecated'))
                continue
            if boundary.status == 'NOT_FOUND':
                if fqn == task.old_api_fqn:
                    trace.append(_trace_node(
                        fqn=fqn, path_score=cur_score, boundary='NOT_FOUND',
                        dead_reason='not_found_root'))
                    return Result(task=task, status='NOT_FOUND', trace=trace)
                # 迭代分支候选在 vpost 必存在（来自 vpost 候选全集），NOT_FOUND 理论不发生
                trace.append(_trace_node(
                    fqn=fqn, path_score=cur_score, boundary='NOT_FOUND',
                    dead_reason='not_found'))
                continue

            resolved = resolve_api(fqn, boundary.vpre, pos,
                                   task.api_type, kb, provider)
            if resolved.kind == 'alias_external':
                # 别名指向外部 API -> 空推荐死分支
                trace.append(_trace_node(
                    fqn=fqn, path_score=cur_score, boundary=boundary.status,
                    vpre=boundary.vpre, vpost=boundary.vpost,
                    resolved_fqn=resolved.resolved_fqn,
                    resolved_kind=resolved.kind, dead_reason='alias_external'))
                continue

            cands = recommend(resolved, boundary.vpre, boundary.vpost,
                              task.api_type, kb, provider, task.top_k,
                              repr_cache=repr_cache)
            if not cands:
                # 检索为空 -> 死分支
                trace.append(_trace_node(
                    fqn=fqn, path_score=cur_score, boundary=boundary.status,
                    vpre=boundary.vpre, vpost=boundary.vpost,
                    resolved_fqn=resolved.resolved_fqn,
                    resolved_kind=resolved.kind, dead_reason='empty_recommend'))
                continue

            # 正常展开：记录该跳候选及其去向分类
            cand_items: List[dict] = []
            entered_final: List[str] = []
            entered_iter: List[str] = []
            pruned: List[str] = []
            for c in cands:
                cand_items.append(
                    {"fqn": c.fqn, "similarity": round(c.similarity, 6)})
                new_path = path + [c.fqn]
                new_scores = scores + [c.similarity]
                new_score = _path_score(new_scores)
                if kb.exists(c.fqn, task.target_version):
                    # 有效候选（∈ Vt）：入 final 并更新下界堆
                    entered_final.append(c.fqn)
                    final.append(Candidate(
                        fqn=c.fqn, api_type=c.api_type, similarity=0.0,
                        evolution_path=new_path, local_scores=new_scores))
                    heapq.heappush(final_heap, new_score)
                    if len(final_heap) > task.top_k:
                        heapq.heappop(final_heap)
                else:
                    # 剪枝：中间节点分数已低于下界 ⇒ 其后代也都低于下界，不入队
                    if len(final_heap) >= task.top_k and new_score < final_heap[0]:
                        pruned.append(c.fqn)
                        continue
                    entered_iter.append(c.fqn)
                    seq += 1
                    heapq.heappush(
                        queue, (-new_score, seq, c.fqn, boundary.vpost,
                                new_path, new_scores))
            trace.append(_trace_node(
                fqn=fqn, path_score=cur_score, boundary=boundary.status,
                vpre=boundary.vpre, vpost=boundary.vpost,
                resolved_fqn=resolved.resolved_fqn,
                resolved_kind=resolved.kind, candidates=cand_items,
                entered_final=entered_final, entered_iter=entered_iter,
                pruned=pruned,
                prune_bound=(final_heap[0]
                             if len(final_heap) >= task.top_k else None)))

        if not final:
            return Result(task=task, status='NO_CANDIDATE', trace=trace)

        # 汇总：按 fqn 合并去重，保留路径评分最高者
        merged: dict[str, Candidate] = {}
        for c in final:
            score = _final_score(c)
            if c.fqn not in merged or score > _final_score(merged[c.fqn]):
                c.similarity = score  # 写入 final_score
                merged[c.fqn] = c
        ranked = sorted(merged.values(), key=_final_score, reverse=True)[: task.top_k]
        return Result(task=task, status='OK', candidates=ranked, trace=trace)

    except Exception as e:
        print(f"[pipeline] 分析失败: {type(e).__name__}: {e}")
        return Result(task=task, status='ERROR',
                      error=f"{type(e).__name__}: {e}", trace=trace)
    finally:
        if own_provider:
            provider.close()
