## @package resolveApi
#  调整待分析对象：解析实质 API（薄入口）
#  职责：get_boundary 给出 (Vpre, Vpost) 后，把 apiX 在 Vpre 更正为其实质 API——
#  定位 apiX 的源码 AST 节点，赋值别名追 value、定义则识别转发/嵌套调用追被调
#  API，并沿 import 链递归追到 FunctionDef/ClassDef（拿到定义）或库外（空推荐）。
#  对应设计文档 §8 流程 (b)。
#
#  本模块只保留薄入口 `resolve_api`，全部解析逻辑委托
#  [Adjust/importResolver.py](importResolver.py) 的 `ImportResolver`：
#  旧实现的「层 1-4 同名近似匹配」（层 2 预设同模块、层 3 全局同名唯一匹配）
#  已被「源码 AST 精确 import/定义定位」取代，彻底移除近似兜底（宁漏荐不错荐）。

from typing import Optional, Set

from Adjust.importResolver import ImportResolver
from Tool.model import KnowledgeBase, ResolvedApi
from Tool.tool import SourceProvider


def resolve_api(fqn: str, version: str, source_version: str, api_type: str,
                kb: KnowledgeBase, provider: SourceProvider,
                visited: Optional[Set[str]] = None) -> ResolvedApi:
    """把 fqn 更正为实质 API（赋值/嵌套调用/import 链递归，AST 精确定位 + 防环）。

    核心解析基于库源码 AST：`ImportResolver.resolve` 定位 fqn 的节点，赋值别名
    追 value、定义则识别转发/嵌套调用追被调 API（追目标时校验粒度一致性，跨粒度
    保留原定义），沿 import 链递归到真实定义或库外（解析失败即空推荐，不做同名
    近似兜底）。

    输入参数：
        fqn (str)：待分析 API 完整名（= get_boundary 的 Vpre 中仍存在的 API）。
        version (str)：fqn 所在版本（= vpre，用于定位节点）。
        source_version (str)：fqn 的起点版本（BFS 队列 pos；根节点 = Vs，迭代
            分支 = 上一跳 vpost）。回退定义取该版本下 fqn 的原始定义。
        api_type (str)：'class' | 'function' | 'method'（来自 Task.api_type）。
        kb (KnowledgeBase)：保留参数，核心解析不再依赖（kb 仅 get_boundary /
            recommend 继续使用 alias_of / 存在性判定）。
        provider (SourceProvider)：提供模块 AST（module_ast / locate_module）。
        visited (Optional[Set[str]])：递归防环集合，外部调用无需传入。
    返回值：
        ResolvedApi：kind 语义见 model.ResolvedApi；original_fqn 恒为最外层输入；
            kind 记录最外层第一次更正的类型（递归内部出现 alias_external /
            unknown 时向最外层透传）。
    """
    return ImportResolver(provider).resolve(fqn, version, source_version, api_type,
                                            visited)
