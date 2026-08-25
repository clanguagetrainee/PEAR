## @package direct_baseline
#  DirectBaseline 基准推荐：无版本感知、无 API 调整的直接相似度检索
#  职责：作为对照 PEAR 全链路（resolve_api + BFS 演化 + recommend）性能的基准工具。
#  不做任何版本感知与 API 调整：直接在 Vs 取待分析 API 的原定义作为查询，
#  对 Vt 中全部同类型 API 逐一计算 tokenBased 相似度，按分数降序取 Top-k。
#  相似度算法复用 Recommend.similarity（与 PEAR 主链路同一实现）。

from typing import List

from Tool.model import Candidate
from Tool.tool import SourceProvider
from Recommend.similarity import build_repr, similarity_from_repr


def direct_baseline(old_api_fqn: str, source_version: str, target_version: str,
                    api_type: str, provider: SourceProvider,
                    top_k: int) -> List[Candidate]:
    """DirectBaseline：直接用待分析 API 原定义在 Vt 全库做相似度 Top-k 检索。

    功能：不调用 resolve_api，不做任何版本感知与 API 调整。把 old_api_fqn 在
    source_version 的原定义作为查询，对 target_version 全部同类型 API 逐一计算
    tokenBased 相似度，按分数降序取前 top_k。

    输入参数：
        old_api_fqn (str)：待分析 API 的完整名（internal_fqn）。
        source_version (str)：Vs，取 old_api_fqn 原定义的版本。
        target_version (str)：Vt，候选检索发生地。
        api_type (str)：'class' | 'function' | 'method'，候选同类型粒度。
        provider (SourceProvider)：定义读取（get_api）+ 候选全集（list_api）。
        top_k (int)：最终返回候选数。
    返回值：
        List[Candidate]：按 similarity 降序的 Top-k；candidate.fqn 为候选完整名，
            api_type = 入参同 type，evolution_path/local_scores 留空。
            与 PEAR 主链路不同，候选全集**不排除** old_api_fqn 自身——若该 API 在
            Vt 仍存在，它将以相似度 1.0 排首位。
    异常：
        ValueError：old_api_fqn 在 source_version 无定义（provider.get_api 返回
            None），或 api_type 非法（由 provider.ensure_batch 抛出）。
    """
    # 1) Vs 取原定义作为查询（测试用例保证存在，缺失即输入错误 -> 显式报错）
    query_def = provider.get_api(old_api_fqn, api_type, source_version)
    if query_def is None:
        raise ValueError(
            f"待分析 API 在 Vs({source_version}) 无定义: {old_api_fqn}")

    # 2) Vt 全部同类型 API 逐一比对（不排除自身，按用户确认「所有」）
    query_repr = build_repr(query_def)
    scored: List[Candidate] = []
    for fqn in provider.list_api(api_type, target_version):
        def_c = provider.get_api(fqn, api_type, target_version)
        if def_c is None:
            # 候选无源码定义，跳过（与 recommend 一致）
            continue
        cand_repr = build_repr(def_c)
        sim = similarity_from_repr(query_repr, cand_repr)
        scored.append(Candidate(fqn=fqn, api_type=api_type, similarity=sim))

    # 3) 降序取 Top-k
    scored.sort(key=lambda c: c.similarity, reverse=True)
    return scored[:top_k]
