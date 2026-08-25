## @package getBoundary
#  调整待分析对象：定位相邻弃用版本对
#  职责：给定待分析 API 与起始位置，在版本序列上顺序判定存在性（只查知识库，
#  不碰源码），定位实际失效相邻版本对 (Vpre, Vpost)。对应设计文档 §8 流程 (a)。
#  存在性判定含别名行——kb 解析时 A: 行也作为记录，fqn 以"别名形态"存在的版本
#  exists 同样为 True；边界落在名字（定义或别名形态都消失）第一次彻底不存在的版本。

from typing import List

from Tool.model import BoundaryResult, KnowledgeBase


def get_boundary(fqn: str, start_version: str, target_version: str,
                 kb: KnowledgeBase, versions: List[str]) -> BoundaryResult:
    """定位 fqn 从 start_version 起到 target_version 的失效相邻版本对。

    输入参数：
        fqn (str)：待查 API 完整名（初始 = 用户 old_api_fqn；迭代 = 候选 FQN）。
        start_version (str)：检查起点版本（初始 = Vs；迭代 = 上一分支的 vpost）。
        target_version (str)：检查终点版本 Vt。
        kb (KnowledgeBase)：存在性判定数据源。
        versions (List[str])：升序版本序列（start 与 target 均须在其中）。
    返回值：
        BoundaryResult：
          - DEPRECATED：vpre = 最后一次存在的版本，vpost = 第一次不存在的版本；
          - NOT_DEPRECATED：start..target 全程存在（vpre/vpost 均 None）；
          - NOT_FOUND：start_version 起即不存在（vpre/vpost 均 None）。
    异常：
        ValueError：start_version / target_version 不在 versions 中。
    """
    if start_version not in versions:
        raise ValueError(f"start_version {start_version} 不在版本序列中")
    if target_version not in versions:
        raise ValueError(f"target_version {target_version} 不在版本序列中")

    start_idx = versions.index(start_version)
    target_idx = versions.index(target_version)
    if target_idx < start_idx:
        raise ValueError(f"target_version {target_version} 早于 start_version {start_version}")

    if not kb.exists(fqn, start_version):
        return BoundaryResult(status='NOT_FOUND')

    prev = start_version
    for version in versions[start_idx + 1: target_idx + 1]:
        if kb.exists(fqn, version):
            prev = version
            continue
        # 取第一个失效边界：prev 最后一次存在，version 第一次不存在
        return BoundaryResult(status='DEPRECATED', vpre=prev, vpost=version)
    return BoundaryResult(status='NOT_DEPRECATED')
