## @package model
#  数据模型
#  职责：定义贯穿全流程的数据结构——Task（任务输入）、APIRecord（知识库单条记录）、
#  KnowledgeBase（弃用知识库）、BoundaryResult（失效边界）、ResolvedApi（实质 API
#  调整结果）、Candidate（单跳候选）、Result（最终推荐结果）。
#  纯数据结构，不含业务逻辑，被所有模块 import。对应设计文档 §6 Tool 定位。

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Task:
    """单个分析任务（Configure JSON / CLI 覆盖的统一内部表示）。

    字段：
        lib_name (str)：库名（如 pandas）。
        source_version (str)：当前使用的库版本 Vs。
        target_version (str)：迁移目标库版本 Vt。
        old_api_fqn (str)：待分析的旧 API 完整名（internal_fqn）。
        api_type (str)：用户手动提供的 old_api_fqn 类型
            （'class' | 'function' | 'method'），全程用于同类型比对；
            kb 不推断 type。
        top_k (int)：每轮局部推荐保留的候选数量。
        lib_repo_path (str)：本地已 clone 的库仓库路径（libRepoPath）。
    """
    lib_name: str
    source_version: str
    target_version: str
    old_api_fqn: str
    api_type: str
    top_k: int
    lib_repo_path: str


@dataclass
class APIRecord:
    """知识库单条 API 记录。

    当前阶段（不做公开名识别）：全链路用完整名 internal_fqn，fqn 即完整名；
    type 不进 kb（由 Configure 的 api_type 提供）；alias_of 仅赋值行解析得到，
    供 resolve_api 识别别名形态。

    字段：
        fqn (str)：完整名（internal_fqn，源码真实定义路径）。
        signature (str)：'(self, other, ignore_index=False)->None'（定义行）；
            赋值行为空字符串。
        alias_of (Optional[str])：仅赋值行（A:）存在，指向赋值目标表达式。
    """
    fqn: str
    signature: str = ''
    alias_of: Optional[str] = None


@dataclass
class KnowledgeBase:
    """某库全部版本的内存化知识库（只读）。

    字段：
        lib_name (str)：库名。
        versions (List[str])：升序版本号。
        _records (Dict[str, Dict[str, APIRecord]])：version -> {fqn: record}。
    """
    lib_name: str
    versions: List[str] = field(default_factory=list)
    _records: Dict[str, Dict[str, APIRecord]] = field(default_factory=dict)

    def exists(self, fqn: str, version: str) -> bool:
        """判断 fqn 在 version 是否存在（含别名行记录）。

        输入参数：
            fqn (str)：完整名。
            version (str)：版本号。
        返回值：
            bool：version 的记录中存在该 fqn 则为 True。
        """
        return fqn in self._records.get(version, {})

    def get(self, fqn: str, version: str) -> Optional[APIRecord]:
        """取 fqn 在 version 的记录，不存在返回 None。

        输入参数：
            fqn (str)：完整名。
            version (str)：版本号。
        返回值：
            Optional[APIRecord]：记录；version 无此 fqn 返回 None。
        """
        return self._records.get(version, {}).get(fqn)

    def all_records(self, version: str) -> Dict[str, APIRecord]:
        """取 version 的全部记录（含别名行）。

        输入参数：
            version (str)：版本号。
        返回值：
            Dict[str, APIRecord]：fqn -> 记录；version 无记录时返回空 dict。
        """
        return self._records.get(version, {})


@dataclass
class BoundaryResult:
    """get_boundary 的返回：失效边界三态。

    字段：
        status (str)：'DEPRECATED' | 'NOT_DEPRECATED' | 'NOT_FOUND'。
        vpre (Optional[str])：status=DEPRECATED 时，最后一次存在的版本。
        vpost (Optional[str])：status=DEPRECATED 时，第一次不存在的版本。
    """
    status: str
    vpre: Optional[str] = None
    vpost: Optional[str] = None


@dataclass
class ResolvedApi:
    """resolve_api 的返回：待分析 API 更正到实质 API 后的结果。

    字段：
        original_fqn (str)：输入的 API 完整名（最外层）。
        resolved_fqn (str)：更正后的实质 API FQN（用于相似度比对）。
        kind (str)：'direct' | 'alias' | 'forward' | 'nested'
                   | 'alias_external' | 'nested_external' | 'unknown'。
        definition (Optional[str])：resolved_fqn 在该版本的完整 API 定义
            （装饰器 + 签名 + 实现，ast.unparse 整节点），无则 None。
    """
    original_fqn: str
    resolved_fqn: str
    kind: str
    definition: Optional[str] = None


@dataclass
class Candidate:
    """单跳推荐结果（Pipeline 汇总前/后的中间形态）。

    字段：
        fqn (str)：候选 FQN。
        api_type (str)：与源同 type。
        similarity (float)：本跳 token 相似度 [0,1]；Pipeline 汇总后改写为
            路径评分（∏ 各跳相似度），作为最终排序依据（final_score）。
        evolution_path (List[str])：到本候选为止的演化路径（含本候选）。
        local_scores (List[float])：路径上每一跳的相似度（与 path 对齐）。
    """
    fqn: str
    api_type: str
    similarity: float = 0.0
    evolution_path: List[str] = field(default_factory=list)
    local_scores: List[float] = field(default_factory=list)


@dataclass
class Result:
    """最终输出（run_pipeline 的返回）。

    字段：
        task (Task)：分析任务。
        status (str)：'OK' | 'NOT_DEPRECATED' | 'NOT_FOUND' | 'NO_CANDIDATE' | 'ERROR'。
        candidates (List[Candidate])：已按最终评分降序、去重后的候选列表。
        error (Optional[str])：status=ERROR 时的错误描述。
    """
    task: Task
    status: str
    candidates: List[Candidate] = field(default_factory=list)
    error: Optional[str] = None
