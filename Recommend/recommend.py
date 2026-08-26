## @package recommend
#  关联替代分析：局部 Top-k 推荐
#  职责：在相邻版本对 (Vpre, Vpost) 内，以调整后的 API 完整定义为基准，
#  在 Vpost 全库范围内检索同类型候选（Class↔Class、Function↔Function、
#  Method↔Method），不做初筛，对候选全集逐一计算相似度，按分数降序取 Top-k。
#  对应设计文档 §8 流程 (c)。

from typing import List, Optional

from Tool.model import Candidate, KnowledgeBase, ResolvedApi
from Tool.tool import SourceProvider
from Recommend.similarity import build_repr, similarity_from_repr


def recommend(resolved: ResolvedApi, from_version: str, to_version: str,
              api_type: str, kb: KnowledgeBase, provider: SourceProvider,
              top_k: int, repr_cache: Optional[dict] = None) -> List[Candidate]:
    """在 to_version 全库同类型 API 中推荐 Top-k 候选。

    输入参数：
        resolved (ResolvedApi)：resolve_api 的输出（原 API 及其完整定义）。
        from_version (str)：vpre（用于排除原 API 自身，当前版本通过 provider 批次隐含）。
        to_version (str)：vpost（检索发生地）。
        kb (KnowledgeBase)：to_version 的记录（候选 fqn 全集来源之一）。
        provider (SourceProvider)：候选定义从缓存读取 + 候选 fqn 全集。
        top_k (int)：最终返回候选数。
        repr_cache (Optional[dict])：调用方传入的候选 tokenize 复用缓存，
            键 (api_type, version) -> {fqn: repr}。BFS 沿同一 vpost 反复检索时，
            候选的 build_repr（ast.parse + tokenize）只做一次。None 时不缓存，
            行为与原实现完全一致（向后兼容）。
    返回值：
        List[Candidate]：按 similarity 降序的 Top-k；candidate.fqn 为候选完整名，
            api_type = resolved 同 type，evolution_path/local_scores 留空由 Pipeline 填。
    异常：
        ValueError：api_type 非法（由 provider.ensure_batch 抛出）。
    """
    # 整批生成该版本该粒度定义（幂等）
    provider.ensure_batch(api_type, to_version)
    all_fqns = provider.list_api(api_type, to_version)

    # 原 API 定义缺失时不参与比对，直接返回空（无有效候选）
    if not resolved.definition:
        return []

    # 候选 repr 复用：同一 (api_type, to_version) 的全量候选 tokenize 结果只缓存一次，
    # BFS 后续分支对同一 vpost 检索时直接复用，避免重复 ast.parse + tokenize。
    # 缓存存「全量候选」repr（不剔除 original_fqn），因为每次调用的 original_fqn
    # 可能不同，剔除必须在每次比对时按当前 original_fqn 做，不能烧进缓存。
    cache_key = (api_type, to_version)
    if repr_cache is not None and cache_key in repr_cache:
        cand_reprs = repr_cache[cache_key]
    else:
        cand_reprs: dict = {}
        for fqn in all_fqns:
            def_c = provider.get_api(fqn, api_type, to_version)
            if def_c is None:
                # 提取失败跳过（批次已生成仍缺文件 = 该 API 无源码定义）
                continue
            cand_reprs[fqn] = build_repr(def_c)
        if repr_cache is not None:
            repr_cache[cache_key] = cand_reprs

    # 不做初筛：候选全集逐一读定义、与完整定义比对（用户确认 ⑦）
    # 剔除「被替代的旧 API」自身（original_fqn）。注意不能用 resolved_fqn：
    # 更正后的实质 API（如 new_api）正是要找回的候选，它以相似度 1.0 入选，不能排除。
    query_repr = build_repr(resolved.definition)
    scored: List[Candidate] = []
    for fqn, cand_repr in cand_reprs.items():
        if fqn == resolved.original_fqn:
            continue
        sim = similarity_from_repr(query_repr, cand_repr)
        scored.append(Candidate(fqn=fqn, api_type=api_type, similarity=sim))

    scored.sort(key=lambda c: c.similarity, reverse=True)
    return scored[:top_k]
