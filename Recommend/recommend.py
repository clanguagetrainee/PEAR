## @package recommend
#  关联替代分析：局部 Top-k 推荐
#  职责：在相邻版本对 (Vpre, Vpost) 内，以调整后的 API 完整定义为基准，
#  在 Vpost 全库范围内检索同类型候选（Class↔Class、Function↔Function、
#  Method↔Method），不做初筛，对候选全集逐一计算相似度，按分数降序取 Top-k。
#  对应设计文档 §8 流程 (c)。

from typing import List

from Tool.model import Candidate, KnowledgeBase, ResolvedApi
from Tool.tool import SourceProvider
from Recommend.similarity import build_repr, similarity_from_repr


def recommend(resolved: ResolvedApi, from_version: str, to_version: str,
              api_type: str, kb: KnowledgeBase, provider: SourceProvider,
              top_k: int) -> List[Candidate]:
    """在 to_version 全库同类型 API 中推荐 Top-k 候选。

    输入参数：
        resolved (ResolvedApi)：resolve_api 的输出（原 API 及其完整定义）。
        from_version (str)：vpre（用于排除原 API 自身，当前版本通过 provider 批次隐含）。
        to_version (str)：vpost（检索发生地）。
        kb (KnowledgeBase)：to_version 的记录（候选 fqn 全集来源之一）。
        provider (SourceProvider)：候选定义从缓存读取 + 候选 fqn 全集。
        top_k (int)：最终返回候选数。
    返回值：
        List[Candidate]：按 similarity 降序的 Top-k；candidate.fqn 为候选完整名，
            api_type = resolved 同 type，evolution_path/local_scores 留空由 Pipeline 填。
    异常：
        ValueError：api_type 非法（由 provider.ensure_batch 抛出）。
    """
    # 整批生成该版本该粒度定义（幂等）
    provider.ensure_batch(api_type, to_version)
    cands_all = provider.list_api(api_type, to_version)

    # 剔除「被替代的旧 API」自身（original_fqn）。
    # 注意不能用 resolved_fqn：更正后的实质 API（如 new_api）正是要找回的候选，
    # 它在 to_version 存在时应以相似度 1.0 入选，不能排除。
    cands_all = [c for c in cands_all if c != resolved.original_fqn]

    # 原 API 定义缺失时不参与比对，直接返回空（无有效候选）
    if not resolved.definition:
        return []

    # 不做初筛：候选全集逐一读定义、与完整定义比对（用户确认 ⑦）
    query_repr = build_repr(resolved.definition)
    scored: List[Candidate] = []
    for fqn in cands_all:
        def_c = provider.get_api(fqn, api_type, to_version)
        if def_c is None:
            # 提取失败跳过（批次已生成仍缺文件 = 该 API 无源码定义）
            continue
        cand_repr = build_repr(def_c)
        sim = similarity_from_repr(query_repr, cand_repr)
        scored.append(Candidate(fqn=fqn, api_type=api_type, similarity=sim))

    scored.sort(key=lambda c: c.similarity, reverse=True)
    return scored[:top_k]
