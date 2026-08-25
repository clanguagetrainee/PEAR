## @package tokenBased
#  关联替代分析：token-based 相似度算法（从 Rbench 复制，勿改动算法本体）
#  来源：/media/he/Rbench/similarity/similarity/tokenBased.py（设计决策 ⑥）
#  职责：将 API 源码（函数/类定义）编码为加权 token 序列，计算加权 Jaccard 相似度。
#  对外入口：similarity(source_a, source_b) 一次性比较；
#            build_representation + similarity_from_representation 两阶段复用。
#  本文件为复制件，仅供 Recommend/similarity.py 薄封装委托，不直接在本项目内改动算法。

import ast
import re
import textwrap
from collections import Counter
from typing import List, Optional, Any, Dict

_API_DEF_RE = re.compile(r"(?:async\s+def|def|class)\b")
def split_name(name: str) -> List[str]:
    """
    功能：
        将标识符拆分为小写单词列表。
        支持的命名风格包括：
        - snake_case
        - camelCase
        - PascalCase

    参数说明：
        name:
            原始标识符字符串。

    返回：
        拆分后的单词列表，全部为小写。
        如果输入为空字符串，则返回空列表。

    示例：
        "getUserName" -> ["get", "user", "name"]
        "user_id" -> ["user", "id"]
    """
    if not name:
        return []

    # 把 camelCase / PascalCase 中的小写-大写边界替换成下划线
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)

    # 按下划线或非单词字符切分，并统一转小写
    parts = re.split(r"[_\W]+", name.lower())

    # 过滤空串
    return [p for p in parts if p]


class APITokenizer(ast.NodeVisitor):
    """
    功能：
        将 Python API 源码（函数 / 类定义）编码为 token 序列。

    设计目标：
        - 对源码做粗粒度的结构化表示
        - 弱化局部变量名、字面细节对相似度的影响
        - 显式抽取“签名信息”，便于做签名加权相似度

    当前会重点编码的信息包括：
        - 函数/类定义类型
        - 函数名 / 类名（拆词后编码）
        - 参数结构
        - 参数类型注解
        - 返回类型注解
        - decorator
        - 控制流 / 调用 / 字面量 / 容器等函数体结构

    使用方式：
        tokenizer = APITokenizer()
        tokens = tokenizer.tokenize(source_code)
    """

    def __init__(self) -> None:
        """
        功能：
            初始化 tokenizer。

        成员变量：
            tokens:
                保存当前源码被编码后的 token 列表。
        """
        self.tokens: List[str] = []

    def emit(self, *items: str) -> None:
        """
        功能：
            向 token 序列追加一个或多个 token。

        参数说明：
            *items:
                任意数量的 token 字符串。

        返回：
            None
        """
        for item in items:
            self.tokens.append(item)

    # ---------- public ----------

    def tokenize(self, source: str) -> List[str]:
        """
        功能：
            将源码字符串转换为 token 列表。

        处理流程：
            1. 去掉公共缩进和首尾空白
            2. 用 ast.parse 解析源码
            3. 如果顶层是类定义，则走类编码逻辑
            4. 如果顶层是函数定义，则走函数编码逻辑
            5. 否则对整个模块 body 做通用遍历

        参数说明：
            source:
                Python 源码字符串。通常是单个函数或类定义源码。

        返回：
            token 列表。
            如果源码为空或 AST 解析后没有 body，则返回空列表。
        """
        source = normalize_source(source)
        if not source:
            return []

        try:
            tree = ast.parse(source)
        except SyntaxError:
            try:
                from .py2_to_py3_converter import convert_py2_to_py3
                py3_source = convert_py2_to_py3(source)
                tree = ast.parse(py3_source)
            except Exception:
                # 转换失败则返回空列表
                return []
        
        self.tokens = []

        if not tree.body:
            return []

        node = tree.body[0]

        if isinstance(node, ast.ClassDef):
            self._encode_class(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._encode_function(node, is_method=False)
        else:
            # 兜底逻辑：不是单独的类/函数定义时，遍历整个模块
            self.emit("API_BEGIN", "BODY_BEGIN")
            for stmt in tree.body:
                self.visit(stmt)
            self.emit("BODY_END", "API_END")

        return self.tokens

    # ---------- top-level encoders ----------

    def _encode_class(self, node: ast.ClassDef) -> None:
        """
        功能：
            编码类定义节点。

        编码内容包括：
            - 类定义标记
            - 类名拆词（作为签名级别 token）
            - 基类名
            - decorator
            - 类体中的属性、__init__、其他方法

        参数说明：
            node:
                ast.ClassDef 节点，表示一个类定义。

        返回：
            None
        """
        self.emit("API_BEGIN", "DEF_CLASS")

        # 类名作为签名信息的一部分，拆成词后编码
        for word in split_name(node.name)[:3]:
            self.emit(f"SIG_NAME_{word}")

        # 编码最多前两个基类
        for base in node.bases[:2]:
            base_name = self._get_name(base)
            if base_name:
                self.emit(f"BASE_{base_name.lower()}")

        # 编码 decorator
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            dname = self._get_name(target)
            if dname:
                self.emit(f"DECORATOR_{dname.lower()}")

        self.emit("BODY_BEGIN")

        init_methods = []
        other_methods = []
        other_stmts = []

        # 将类体分为：
        # 1. __init__
        # 2. 其他方法
        # 3. 其他语句（类属性、赋值等）
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name == "__init__":
                    init_methods.append(item)
                else:
                    other_methods.append(item)
            else:
                other_stmts.append(item)

        # 先遍历类属性/其他语句
        for stmt in other_stmts:
            self.visit(stmt)

        # 优先编码 __init__
        for m in init_methods:
            self._encode_function(m, is_method=True)

        # 其他方法按名字排序，保证 token 输出更稳定
        for m in sorted(other_methods, key=lambda x: x.name):
            self._encode_function(m, is_method=True)

        self.emit("BODY_END", "API_END")

    def _encode_function(self, node: ast.AST, is_method: bool) -> None:
        """
        功能：
            编码函数或方法定义。

        编码内容包括：
            - 定义类型（函数 / 方法 / __init__ / async 版本）
            - decorator
            - 函数名（签名信息）
            - 参数结构
            - 参数类型注解（签名信息）
            - 返回类型注解（签名信息）
            - 默认值字面量
            - 函数体结构

        参数说明：
            node:
                ast.FunctionDef 或 ast.AsyncFunctionDef 节点。

            is_method:
                是否把该定义视为类方法。
                True 表示类中的方法；
                False 表示顶层函数。

        返回：
            None
        """
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))

        # 编码定义类型
        if node.name == "__init__":
            self.emit("DEF_INIT")
        elif isinstance(node, ast.AsyncFunctionDef):
            self.emit("DEF_ASYNC_METHOD" if is_method else "DEF_ASYNC_FUNC")
        elif is_method:
            self.emit("DEF_METHOD")
        else:
            self.emit("DEF_FUNC")

        # 编码 decorator
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            dname = self._get_name(target)
            if dname:
                self.emit(f"DECORATOR_{dname.lower()}")

        # ---------- 签名信息：函数名 ----------
        for word in split_name(node.name)[:3]:
            self.emit(f"SIG_NAME_{word}")

        # ---------- 签名信息：参数 ----------
        args = node.args
        all_pos_args = args.posonlyargs + args.args

        # 处理位置参数 / 普通参数
        for arg in all_pos_args:
            if arg.arg == "self":
                self.emit("SELF_PARAM")
            elif arg.arg == "cls":
                self.emit("CLS_PARAM")
            else:
                self.emit("PARAM")

            if arg.annotation is not None:
                for tok in self._annotation_to_tokens(arg.annotation, prefix="PARAM_TYPE"):
                    self.emit(tok)

        # 处理 *args
        if args.vararg:
            self.emit("PARAM_VARARG")
            if args.vararg.annotation is not None:
                for tok in self._annotation_to_tokens(args.vararg.annotation, prefix="PARAM_TYPE"):
                    self.emit(tok)

        # 处理 keyword-only 参数
        for kwarg in args.kwonlyargs:
            self.emit("PARAM")
            if kwarg.annotation is not None:
                for tok in self._annotation_to_tokens(kwarg.annotation, prefix="PARAM_TYPE"):
                    self.emit(tok)

        # 处理 **kwargs
        if args.kwarg:
            self.emit("PARAM_KWARG")
            if args.kwarg.annotation is not None:
                for tok in self._annotation_to_tokens(args.kwarg.annotation, prefix="PARAM_TYPE"):
                    self.emit(tok)

        # ---------- 签名信息：返回类型 ----------
        if node.returns is not None:
            for tok in self._annotation_to_tokens(node.returns, prefix="RET_TYPE"):
                self.emit(tok)

        # ---------- 默认值 ----------
        for d in args.defaults:
            self._emit_literal_token(d)

        for d in args.kw_defaults:
            if d is not None:
                self._emit_literal_token(d)

        # ---------- 函数体 ----------
        self.emit("BODY_BEGIN")
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue
            self.visit(stmt)
        self.emit("BODY_END")

    # ---------- visitors ----------

    def visit_Return(self, node: ast.Return) -> None:
        """
        功能：
            访问 return 语句。

        参数说明：
            node:
                ast.Return 节点。

        返回：
            None
        """
        self.emit("RETURN")
        if node.value:
            self.visit(node.value)

    def visit_Raise(self, node: ast.Raise) -> None:
        """
        功能：
            访问 raise 语句。

        参数说明：
            node:
                ast.Raise 节点。

        返回：
            None
        """
        self.emit("RAISE")
        if node.exc:
            self.visit(node.exc)

    def visit_Assign(self, node: ast.Assign) -> None:
        """
        功能：
            访问普通赋值语句。

        参数说明：
            node:
                ast.Assign 节点。

        返回：
            None
        """
        self.emit("ASSIGN")
        for t in node.targets:
            self.visit(t)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """
        功能：
            访问带类型注解的赋值语句。

        参数说明：
            node:
                ast.AnnAssign 节点。

        返回：
            None
        """
        self.emit("ANN_ASSIGN")
        self.visit(node.target)
        if node.value:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """
        功能：
            访问增量赋值语句，例如 x += 1。

        参数说明：
            node:
                ast.AugAssign 节点。

        返回：
            None
        """
        self.emit("AUG_ASSIGN")
        self.visit(node.target)
        self.visit(node.op)
        self.visit(node.value)

    def visit_If(self, node: ast.If) -> None:
        """
        功能：
            访问 if / elif / else 结构。

        参数说明：
            node:
                ast.If 节点。

        返回：
            None
        """
        self.emit("IF")
        self.visit(node.test)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_For(self, node: ast.For) -> None:
        """
        功能：
            访问 for 循环。

        参数说明：
            node:
                ast.For 节点。

        返回：
            None
        """
        self.emit("FOR")
        self.visit(node.target)
        self.visit(node.iter)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """
        功能：
            访问 async for 循环。

        参数说明：
            node:
                ast.AsyncFor 节点。

        返回：
            None
        """
        self.emit("FOR")
        self.visit(node.target)
        self.visit(node.iter)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_While(self, node: ast.While) -> None:
        """
        功能：
            访问 while 循环。

        参数说明：
            node:
                ast.While 节点。

        返回：
            None
        """
        self.emit("WHILE")
        self.visit(node.test)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Try(self, node: ast.Try) -> None:
        """
        功能：
            访问 try / except / else / finally 结构。

        参数说明：
            node:
                ast.Try 节点。

        返回：
            None
        """
        self.emit("TRY")
        for stmt in node.body:
            self.visit(stmt)

        for h in node.handlers:
            self.emit("EXCEPT")
            if h.type:
                self.visit(h.type)
            for stmt in h.body:
                self.visit(stmt)

        for stmt in node.orelse:
            self.visit(stmt)

        for stmt in node.finalbody:
            self.visit(stmt)

    def visit_With(self, node: ast.With) -> None:
        """
        功能：
            访问 with 语句。

        参数说明：
            node:
                ast.With 节点。

        返回：
            None
        """
        self.emit("WITH")
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.visit(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:
        """
        功能：
            访问函数调用表达式。

        编码内容包括：
            - 通用调用标记 CALL
            - 如果能识别调用目标名，则额外发出 CALL_xxx

        参数说明：
            node:
                ast.Call 节点。

        返回：
            None
        """
        self.emit("CALL")

        call_name = self._get_call_name(node.func)
        if call_name:
            self.emit(f"CALL_{call_name.lower()}")

        self.visit(node.func)

        for arg in node.args:
            self.visit(arg)

        for kw in node.keywords:
            self.visit(kw.value)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """
        功能：
            访问属性访问表达式。

        编码规则：
            - self.xxx -> SELF_ATTR
            - cls.xxx -> CLASS_ATTR
            - 其他对象属性 -> ATTR

        参数说明：
            node:
                ast.Attribute 节点。

        返回：
            None
        """
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            self.emit("SELF_ATTR")
        elif isinstance(node.value, ast.Name) and node.value.id == "cls":
            self.emit("CLASS_ATTR")
        else:
            self.emit("ATTR")
            self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """
        功能：
            访问下标/切片表达式，例如 a[i]、list[int]。

        参数说明：
            node:
                ast.Subscript 节点。

        返回：
            None
        """
        self.emit("SUBSCRIPT")
        self.visit(node.value)
        self.visit(node.slice)

    def visit_Name(self, node: ast.Name) -> None:
        """
        功能：
            访问变量名。

        编码规则：
            - self -> SELF_PARAM
            - cls -> CLS_PARAM
            - 其他名字 -> VAR

        参数说明：
            node:
                ast.Name 节点。

        返回：
            None
        """
        if node.id == "self":
            self.emit("SELF_PARAM")
        elif node.id == "cls":
            self.emit("CLS_PARAM")
        else:
            self.emit("VAR")

    def visit_Constant(self, node: ast.Constant) -> None:
        """
        功能：
            访问字面量常量节点。

        参数说明：
            node:
                ast.Constant 节点。

        返回：
            None
        """
        self._emit_constant(node.value)

    def visit_List(self, node: ast.List) -> None:
        """
        功能：
            访问列表字面量。

        参数说明：
            node:
                ast.List 节点。

        返回：
            None
        """
        self.emit("LIST")
        for elt in node.elts:
            self.visit(elt)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        """
        功能：
            访问元组字面量。

        参数说明：
            node:
                ast.Tuple 节点。

        返回：
            None
        """
        self.emit("TUPLE")
        for elt in node.elts:
            self.visit(elt)

    def visit_Dict(self, node: ast.Dict) -> None:
        """
        功能：
            访问字典字面量。

        参数说明：
            node:
                ast.Dict 节点。

        返回：
            None
        """
        self.emit("DICT")
        for k, v in zip(node.keys, node.values):
            if k:
                self.visit(k)
            if v:
                self.visit(v)

    def visit_Set(self, node: ast.Set) -> None:
        """
        功能：
            访问集合字面量。

        参数说明：
            node:
                ast.Set 节点。

        返回：
            None
        """
        self.emit("SET")
        for elt in node.elts:
            self.visit(elt)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """
        功能：
            访问列表推导式。

        参数说明：
            node:
                ast.ListComp 节点。

        返回：
            None
        """
        self.emit("COMPREHENSION")
        self.visit(node.elt)
        for gen in node.generators:
            self.visit(gen.iter)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """
        功能：
            访问字典推导式。

        参数说明：
            node:
                ast.DictComp 节点。

        返回：
            None
        """
        self.emit("COMPREHENSION")
        self.visit(node.key)
        self.visit(node.value)
        for gen in node.generators:
            self.visit(gen.iter)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """
        功能：
            访问集合推导式。

        参数说明：
            node:
                ast.SetComp 节点。

        返回：
            None
        """
        self.emit("COMPREHENSION")
        self.visit(node.elt)
        for gen in node.generators:
            self.visit(gen.iter)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """
        功能：
            访问生成器表达式。

        参数说明：
            node:
                ast.GeneratorExp 节点。

        返回：
            None
        """
        self.emit("COMPREHENSION")
        self.visit(node.elt)
        for gen in node.generators:
            self.visit(gen.iter)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """
        功能：
            访问 lambda 表达式。

        参数说明：
            node:
                ast.Lambda 节点。

        返回：
            None
        """
        self.emit("LAMBDA")
        self.visit(node.body)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        """
        功能：
            访问布尔运算表达式，例如 and / or。

        参数说明：
            node:
                ast.BoolOp 节点。

        返回：
            None
        """
        self.emit("BOOL_AND" if isinstance(node.op, ast.And) else "BOOL_OR")
        for v in node.values:
            self.visit(v)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        """
        功能：
            访问一元运算表达式。

        当前会显式区分 not。

        参数说明：
            node:
                ast.UnaryOp 节点。

        返回：
            None
        """
        if isinstance(node.op, ast.Not):
            self.emit("UNARY_NOT")
        self.visit(node.operand)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        """
        功能：
            访问二元运算表达式，例如加减乘除。

        参数说明：
            node:
                ast.BinOp 节点。

        返回：
            None
        """
        op_map = {
            ast.Add: "BIN_ADD",
            ast.Sub: "BIN_SUB",
            ast.Mult: "BIN_MUL",
            ast.Div: "BIN_DIV",
            ast.Mod: "BIN_MOD",
        }
        self.emit(op_map.get(type(node.op), "BIN_OP"))
        self.visit(node.left)
        self.visit(node.right)

    def visit_Compare(self, node: ast.Compare) -> None:
        """
        功能：
            访问比较表达式。

        支持编码：
            ==, !=, <, <=, >, >=, is, is not, in, not in

        参数说明：
            node:
                ast.Compare 节点。

        返回：
            None
        """
        op_map = {
            ast.Eq: "COMPARE_EQ",
            ast.NotEq: "COMPARE_NE",
            ast.Lt: "COMPARE_LT",
            ast.LtE: "COMPARE_LE",
            ast.Gt: "COMPARE_GT",
            ast.GtE: "COMPARE_GE",
            ast.Is: "COMPARE_IS",
            ast.IsNot: "COMPARE_IS_NOT",
            ast.In: "COMPARE_IN",
            ast.NotIn: "COMPARE_NOT_IN",
        }

        self.visit(node.left)
        for op, comp in zip(node.ops, node.comparators):
            self.emit(op_map.get(type(op), "COMPARE"))
            self.visit(comp)

    def generic_visit(self, node: ast.AST) -> None:
        """
        功能：
            默认 AST 访问行为。

        参数说明：
            node:
                任意 AST 节点。

        返回：
            None
        """
        super().generic_visit(node)

    # ---------- helpers ----------

    def _emit_literal_token(self, node: ast.AST) -> None:
        """
        功能：
            为默认值等节点发出字面量 token。

        规则：
            - 如果节点本身是常量，则直接发常量 token
            - 否则递归访问这个 AST 节点

        参数说明：
            node:
                任意 AST 节点，通常是参数默认值节点。

        返回：
            None
        """
        if isinstance(node, ast.Constant):
            self._emit_constant(node.value)
        else:
            self.visit(node)

    def _emit_constant(self, value: Any) -> None:
        """
        功能：
            将 Python 常量值映射为粗粒度字面量 token。

        映射规则：
            - None -> LIT_NONE
            - bool -> LIT_BOOL
            - 0 -> LIT_0
            - 1 -> LIT_1
            - 其他 int -> LIT_INT
            - float -> LIT_FLOAT
            - "" -> LIT_EMPTY_STR
            - 其他 str -> LIT_STR
            - 其他 -> LIT_CONST

        参数说明：
            value:
                Python 常量值。

        返回：
            None
        """
        if value is None:
            self.emit("LIT_NONE")
        elif value is True or value is False:
            self.emit("LIT_BOOL")
        elif isinstance(value, int):
            if value == 0:
                self.emit("LIT_0")
            elif value == 1:
                self.emit("LIT_1")
            else:
                self.emit("LIT_INT")
        elif isinstance(value, float):
            self.emit("LIT_FLOAT")
        elif isinstance(value, str):
            if value == "":
                self.emit("LIT_EMPTY_STR")
            else:
                self.emit("LIT_STR")
        else:
            self.emit("LIT_CONST")

    def _get_name(self, node: ast.AST) -> Optional[str]:
        """
        功能：
            从 Name / Attribute 节点中提取名字。

        例如：
            - foo -> "foo"
            - obj.bar -> "bar"

        参数说明：
            node:
                AST 节点。

        返回：
            识别出的名字字符串；
            若无法识别，则返回 None。
        """
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _get_call_name(self, node: ast.AST) -> Optional[str]:
        """
        功能：
            提取函数调用目标名。

        例如：
            - foo(...) -> "foo"
            - obj.bar(...) -> "bar"

        参数说明：
            node:
                调用目标 AST 节点。

        返回：
            调用目标名；
            若无法识别，则返回 None。
        """
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _annotation_to_tokens(self, node: ast.AST, prefix: str) -> List[str]:
        """
        功能：
            将类型注解 AST 归一化为 token 列表。

        示例：
            int -> [PARAM_TYPE_int]
            Optional[str] -> [PARAM_TYPE_optional, PARAM_TYPE_str]
            list[int] -> [PARAM_TYPE_list, PARAM_TYPE_int]
            A | B -> [PARAM_TYPE_union, PARAM_TYPE_a, PARAM_TYPE_b]

        参数说明：
            node:
                类型注解对应的 AST 节点。

            prefix:
                token 前缀。
                常见值：
                - "PARAM_TYPE"：参数类型
                - "RET_TYPE"：返回类型

        返回：
            归一化后的 token 列表。
            会做去重，并限制最多保留前 6 个 token。
        """
        words: List[str] = []
        self._collect_annotation_words(node, words)

        # 去重，同时保持原有顺序
        deduped: List[str] = []
        seen = set()
        for w in words:
            if w and w not in seen:
                seen.add(w)
                deduped.append(w)

        return [f"{prefix}_{w}" for w in deduped[:6]]

    def _collect_annotation_words(self, node: ast.AST, out: List[str]) -> None:
        """
        功能：
            递归提取类型注解中的名字词元。

        支持的类型注解形式包括：
            - Name: int
            - Attribute: typing.List
            - Subscript: list[int], Optional[str]
            - Tuple/List: tuple[int, str]
            - Union 语法: int | str
            - Forward reference: "User"

        参数说明：
            node:
                类型注解对应的 AST 节点。

            out:
                输出列表，用于收集拆分后的词元。

        返回：
            None
        """
        if isinstance(node, ast.Name):
            out.extend(split_name(node.id))
            return

        if isinstance(node, ast.Attribute):
            out.extend(split_name(node.attr))
            return

        if isinstance(node, ast.Subscript):
            self._collect_annotation_words(node.value, out)
            self._collect_annotation_words(node.slice, out)
            return

        if isinstance(node, ast.Tuple):
            for elt in node.elts:
                self._collect_annotation_words(elt, out)
            return

        if isinstance(node, ast.List):
            for elt in node.elts:
                self._collect_annotation_words(elt, out)
            return

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            out.append("union")
            self._collect_annotation_words(node.left, out)
            self._collect_annotation_words(node.right, out)
            return

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.extend(split_name(node.value))
            return

        name = self._get_name(node)
        if name:
            out.extend(split_name(name))


_API_DEF_RE = re.compile(r"(?:async\s+def|def|class)\b")


def normalize_source(source: str) -> str:
    """
    对输入源码做统一预处理。

    修复点：
    - 不使用 .strip() 直接处理整段源码
    - 按第一个 def / async def / class 行的缩进作为基准缩进
    - 只删除首尾空白行，不破坏有效代码行前导空格
    """
    if not isinstance(source, str):
        raise TypeError(f"source 必须是字符串，当前类型为：{type(source)}")

    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return ""

    base_prefix = ""
    for line in lines:
        stripped = line.lstrip(" \t")
        if _API_DEF_RE.match(stripped):
            base_prefix = line[: len(line) - len(stripped)]
            break

    if base_prefix:
        n = len(base_prefix)
        lines = [
            line[n:] if line.startswith(base_prefix) else line
            for line in lines
        ]
    else:
        lines = textwrap.dedent("\n".join(lines)).split("\n")

    return "\n".join(lines).rstrip()


def token_weight(
    token: str,
    name_weight: float = 3.0,
    param_type_weight: float = 4.0,
    return_type_weight: float = 2.0,
    default_weight: float = 1.0,
) -> float:
    """
    功能：
        为不同 token 分配权重。

    当前策略：
        - 函数名 / 类名签名 token：使用 name_weight
        - 参数类型 token：使用 param_type_weight
        - 返回类型 token：使用 return_type_weight
        - 其余所有 token：使用 default_weight

    参数说明：
        token:
            单个 token 字符串。

        name_weight:
            签名名字 token 的权重。

        param_type_weight:
            参数类型 token 的权重。

        return_type_weight:
            返回类型 token 的权重。

        default_weight:
            普通 token 的默认权重。

    返回：
        该 token 对应的权重值。
    """
    if token.startswith("SIG_NAME_"):
        return name_weight
    if token.startswith("PARAM_TYPE_"):
        return param_type_weight
    if token.startswith("RET_TYPE_"):
        return return_type_weight
    return default_weight


def build_token_counter(tokens: List[str]) -> Counter:
    """
    功能：
        根据 token 列表构建多重集合计数器。

    参数说明：
        tokens:
            token 列表。

    返回：
        Counter 对象。
    """
    return Counter(tokens)


def build_representation(
    source: str,
    name_weight: float = 3.0,
    param_type_weight: float = 4.0,
    return_type_weight: float = 2.0,
    default_weight: float = 1.0,
) -> Dict[str, Any]:
    """
    功能：
        将一段 API 源码预处理并编译为可复用表示。

    设计目的：
        供外部 rank 流程复用。
        一个 query / candidate 只需构建一次表示，
        后续可多次参与 similarity_from_representation 比较，
        避免重复 tokenize 和重复统计 Counter。

    参数说明：
        source:
            输入的 Python 源码字符串。

        name_weight:
            函数名 / 类名签名 token 的权重。

        param_type_weight:
            参数类型 token 的权重。

        return_type_weight:
            返回类型 token 的权重。

        default_weight:
            普通 token 的默认权重。

    返回：
        一个表示字典，格式为：
        {
            "algorithm": "tokenBased",
            "normalized_source": ...,
            "tokens": ...,
            "counter": ...,
            "weights": {
                "name_weight": ...,
                "param_type_weight": ...,
                "return_type_weight": ...,
                "default_weight": ...
            },
            "meta": {
                "token_count": ...,
                "unique_token_count": ...
            }
        }
    """
    normalized_source = normalize_source(source)

    tokenizer = APITokenizer()
    tokens = tokenizer.tokenize(normalized_source)
    counter = build_token_counter(tokens)

    return {
        "algorithm": "tokenBased",
        "normalized_source": normalized_source,
        "tokens": tokens,
        "counter": counter,
        "weights": {
            "name_weight": name_weight,
            "param_type_weight": param_type_weight,
            "return_type_weight": return_type_weight,
            "default_weight": default_weight,
        },
        "meta": {
            "token_count": len(tokens),
            "unique_token_count": len(counter),
        },
    }


def weighted_jaccard_similarity_from_counter(
    counter_a: Counter,
    counter_b: Counter,
    name_weight: float = 3.0,
    param_type_weight: float = 4.0,
    return_type_weight: float = 2.0,
    default_weight: float = 1.0,
) -> float:
    """
    功能：
        使用加权 Jaccard 计算两个 token 多重集合计数器的相似度。

    公式：
        similarity = sum(w(t) * min(count_a(t), count_b(t))) /
                     sum(w(t) * max(count_a(t), count_b(t)))

    参数说明：
        counter_a:
            第一段源码对应的 token Counter。

        counter_b:
            第二段源码对应的 token Counter。

        name_weight:
            签名名字 token 的权重。

        param_type_weight:
            参数类型 token 的权重。

        return_type_weight:
            返回类型 token 的权重。

        default_weight:
            普通 token 的默认权重。

    返回：
        加权 Jaccard 相似度，范围为 [0.0, 1.0]。
        若任一 Counter 为空，则返回 0.0。
    """
    if not counter_a or not counter_b:
        return 0.0

    universe = set(counter_a) | set(counter_b)

    inter = 0.0
    union = 0.0

    for tok in universe:
        w = token_weight(
            tok,
            name_weight=name_weight,
            param_type_weight=param_type_weight,
            return_type_weight=return_type_weight,
            default_weight=default_weight,
        )
        inter += w * min(counter_a[tok], counter_b[tok])
        union += w * max(counter_a[tok], counter_b[tok])

    if union <= 0:
        return 0.0

    return inter / union


def weighted_jaccard_similarity(
    tokens_a: List[str],
    tokens_b: List[str],
    name_weight: float = 3.0,
    param_type_weight: float = 4.0,
    return_type_weight: float = 2.0,
    default_weight: float = 1.0,
) -> float:
    """
    功能：
        使用加权 Jaccard 计算两个 token 多重集合的相似度。

    这里把 token 列表当成“多重集合（multiset）”：
        - token 是否出现重要
        - token 出现次数也重要
        - token 顺序不参与计算

    参数说明：
        tokens_a:
            第一段源码对应的 token 列表。

        tokens_b:
            第二段源码对应的 token 列表。

        name_weight:
            签名名字 token 的权重。

        param_type_weight:
            参数类型 token 的权重。

        return_type_weight:
            返回类型 token 的权重。

        default_weight:
            普通 token 的默认权重。

    返回：
        加权 Jaccard 相似度，范围为 [0.0, 1.0]。
        若任一序列为空，则返回 0.0。
    """
    if not tokens_a or not tokens_b:
        return 0.0

    ca = build_token_counter(tokens_a)
    cb = build_token_counter(tokens_b)

    return weighted_jaccard_similarity_from_counter(
        counter_a=ca,
        counter_b=cb,
        name_weight=name_weight,
        param_type_weight=param_type_weight,
        return_type_weight=return_type_weight,
        default_weight=default_weight,
    )


def similarity_from_representation(
    repr_a: Dict[str, Any],
    repr_b: Dict[str, Any],
) -> float:
    """
    功能：
        基于两段已编译表示计算 tokenBased 相似度。

    设计目的：
        供 rank 的 compile + compare 两阶段流程调用。
        此函数不再重新 tokenize / Counter 化，
        而是直接复用 build_representation 的结果。

    参数说明：
        repr_a:
            第一段源码对应的已编译表示。

        repr_b:
            第二段源码对应的已编译表示。

    返回：
        相似度分数，范围为 [0.0, 1.0]。

    异常：
        - TypeError：输入表示格式不合法
        - ValueError：表示不是 tokenBased 表示
    """
    if not isinstance(repr_a, dict) or not isinstance(repr_b, dict):
        raise TypeError("repr_a 和 repr_b 都必须是 dict 类型的表示对象")

    if repr_a.get("algorithm") != "tokenBased":
        raise ValueError(f"repr_a 不是 tokenBased 表示：{repr_a.get('algorithm')}")
    if repr_b.get("algorithm") != "tokenBased":
        raise ValueError(f"repr_b 不是 tokenBased 表示：{repr_b.get('algorithm')}")

    counter_a = repr_a.get("counter")
    counter_b = repr_b.get("counter")

    if not isinstance(counter_a, Counter):
        raise TypeError("repr_a['counter'] 必须是 Counter")
    if not isinstance(counter_b, Counter):
        raise TypeError("repr_b['counter'] 必须是 Counter")

    # 使用 query 侧表示中固化的权重配置
    weights = repr_a.get("weights", {})
    name_weight = weights.get("name_weight", 3.0)
    param_type_weight = weights.get("param_type_weight", 4.0)
    return_type_weight = weights.get("return_type_weight", 2.0)
    default_weight = weights.get("default_weight", 1.0)

    score = weighted_jaccard_similarity_from_counter(
        counter_a=counter_a,
        counter_b=counter_b,
        name_weight=name_weight,
        param_type_weight=param_type_weight,
        return_type_weight=return_type_weight,
        default_weight=default_weight,
    )

    return max(0.0, min(1.0, float(score)))


def similarity(
    source_a: str,
    source_b: str,
    name_weight: float = 3.0,
    param_type_weight: float = 4.0,
    return_type_weight: float = 2.0,
    default_weight: float = 1.0,
) -> float:
    """
    功能：
        计算两段 API 源码的 tokenBased 相似度。

    说明：
        这是兼容旧调用方式的包装接口。
        内部实现已改为：
            1. build_representation(source_a)
            2. build_representation(source_b)
            3. similarity_from_representation(repr_a, repr_b)

    参数说明：
        source_a:
            第一段 Python 源码字符串。

        source_b:
            第二段 Python 源码字符串。

        name_weight:
            函数名 / 类名签名 token 的权重。

        param_type_weight:
            参数类型 token 的权重。

        return_type_weight:
            返回类型 token 的权重。

        default_weight:
            普通 token 的默认权重。

    返回：
        两段源码的相似度分数，范围为 [0.0, 1.0]。
        若任一源码无法产生有效 token，则返回 0.0。
    """
    repr_a = build_representation(
        source=source_a,
        name_weight=name_weight,
        param_type_weight=param_type_weight,
        return_type_weight=return_type_weight,
        default_weight=default_weight,
    )
    repr_b = build_representation(
        source=source_b,
        name_weight=name_weight,
        param_type_weight=param_type_weight,
        return_type_weight=return_type_weight,
        default_weight=default_weight,
    )

    return similarity_from_representation(repr_a, repr_b)


# func_a = """
# @xxdfa(ist=0.3)
# def get_user_name(user_id: int, default: str = "") -> str:
#     if user_id <= 0:
#         return default
#     return str(user_id)
# """

# func_b = """
# def fetchUserName(uid: int, default: str = "") -> str:
#     if uid <= 0:
#         return default
#     return str(uid)
# """

# print("函数相似度:", similarity(func_a, func_b))

# class_a = """
# class UserService(BaseService):
#     def __init__(self, repo: str):
#         self.repo = repo
#
#     def get_user_name(self, user_id: int) -> str:
#         if user_id == 0:
#             return ""
#         return self.repo
# """

# class_b = """
# class AccountService(BaseService):
#     def __init__(self, storage: str):
#         self.storage = storage
#
#     def fetch_user_name(self, uid: int) -> str:
#         if uid == 0:
#             return ""
#         return self.storage
# """

# print("类相似度:", similarity(class_a, class_b))