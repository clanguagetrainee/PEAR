## @package similarity
#  关联替代分析：token-based 相似度（唯一对外接口）
#  职责：计算两个 API 完整定义文本之间的代码相似度，返回 [0,1] 分数。
#  算法：直接复用本项目复制的 Rbench tokenBased 实现（Recommend/tokenBased.py），
#  不对算法做任何自定义改造（设计决策 ⑥）。
#  相似度输入为整个 API 定义文本（装饰器 + 签名 + 实现），定义空不额外退化
#  （设计决策 ⑧，tokenBased 对空输入自然返回 0.0）。
#  recommend.py 依赖本模块的 token_similarity / build_repr 两阶段接口。

from .tokenBased import (
    build_representation,
    similarity_from_representation,
    similarity as _similarity,
)


def token_similarity(def_a: str, def_b: str) -> float:
    """计算两段完整 API 定义文本的 token 相似度，越大越相似，返回 [0,1]。

    输入参数：
        def_a (str)：原 API 定义文本（resolve_api 的 definition，完整定义）。
        def_b (str)：候选 API 定义文本。
    返回值：
        float：[0,1] 相似度。定义为空/无法产生有效 token 时算法自身返回 0.0，
            不做额外退化处理。
    异常：
        TypeError：def_a / def_b 非字符串时抛出。
    实现：委托 tokenBased.similarity()。
    """
    return _similarity(def_a, def_b)


def build_repr(source: str) -> dict:
    """将 API 完整定义文本编译为可复用表示，供 recommend 对多个候选重复比较。

    输入参数：
        source (str)：API 完整定义文本。
    返回值：
        dict：tokenBased 表示（含 normalized_source / tokens / counter / weights / meta），
            可多次传入 similarity_from_repr 参与比较，避免重复 tokenize。
    异常：
        TypeError：source 非字符串时抛出。
    """
    return build_representation(source)


def similarity_from_repr(repr_a: dict, repr_b: dict) -> float:
    """基于两段已编译表示计算 tokenBased 相似度（复用，不重复 tokenize）。

    输入参数：
        repr_a (dict)：原 API 的已编译表示（build_repr 输出）。
        repr_b (dict)：候选 API 的已编译表示（build_repr 输出）。
    返回值：
        float：[0,1] 相似度。
    异常：
        TypeError：任一输入不是 dict 或 counter 缺失时抛出。
        ValueError：任一表示不是 tokenBased 表示时抛出。
    实现：委托 tokenBased.similarity_from_representation()。
    """
    return similarity_from_representation(repr_a, repr_b)
