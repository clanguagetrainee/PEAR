## @package py2parse
#  Python 2 语法解析兜底
#  职责：ast.parse 无法解析 Py2 语法的老版本源码（print 语句、except A, B:、
#  for (x, y) in 等）时，用 lib2to3 的 2to3 转换将 Py2 代码转为 Py3 后再次
#  ast.parse，最大化老版本（django 1.x / matplotlib 0.x / pandas 0.x）的
#  API 提取覆盖率。lib2to3 为 Python 标准库（3.11/3.13 均可用，仅 deprecated
#  警告），零新增依赖。所有失败路径均显式打印原因并返回 None，由调用方决定
#  是否跳过该文件，不做静默降级。

import ast

from typing import Optional

_2TOOL = None


def _get_2to3_tool():
    """懒加载并复用 lib2to3 2to3 转换工具（避免反复实例化加载全部 fixer）。

    输入参数：
        无。
    返回值：
        lib2to3.refactor.RefactoringTool：2to3 转换工具实例。
    异常：
        ImportError：lib2to3 在当前 Python 不可用时抛出（未来版本若从
            标准库移除）。
    """
    global _2TOOL
    if _2TOOL is None:
        from lib2to3.refactor import RefactoringTool, get_fixers_from_package
        # Python 3.11+ 的 RefactoringTool 需显式传入 fixer 列表（不再默认全部）。
        _2TOOL = RefactoringTool(get_fixers_from_package('lib2to3.fixes'))
    return _2TOOL


def safe_parse(code: str, filename: str = '<unknown>') -> Optional[ast.Module]:
    """解析源码，优先 ast.parse，Py2 语法失败时用 2to3 转换后重试。

    输入参数：
        code (str)：源码文本。
        filename (str)：文件名或标识，用于错误信息，默认 '<unknown>'。
    返回值：
        Optional[ast.Module]：解析成功的 AST 根；Py2 兜底也失败时返回 None
            （不抛出异常，由调用方决定跳过该文件）。
    """
    try:
        return ast.parse(code, filename=filename, mode='exec')
    except (SyntaxError, ValueError) as e:
        print(f"[py2parse] {filename} ast.parse 失败: {type(e).__name__}: {e}，尝试 2to3 转换兜底")
    try:
        tool = _get_2to3_tool()
        # 3.11 下 refactor_string 返回 lib2to3.pytree.Node，须 str() 转回源码文本
        converted = str(tool.refactor_string(code, filename))
        return ast.parse(converted, filename=filename, mode='exec')
    except Exception as e:
        print(f"[py2parse] {filename} Py2 兜底解析失败: {type(e).__name__}: {e}")
        return None
