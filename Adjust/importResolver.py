## @package importResolver
#  源码 AST 精确解析：定位名字来源与定义
#  职责：把「调用/赋值里的名字」精确解析为 internal_fqn（不再用同名近似匹配）。
#  基于 SourceProvider 的模块级 AST：定位 fqn 的 AST 节点（定义/赋值/import），
#  构建模块的名字绑定表（import 表 ∪ 顶层定义），点链逐段下钻，递归追 import 链
#  直到 FunctionDef/ClassDef（拿到定义）或库外（解析失败）。对应设计文档 §8 流程
#  (b) 的「更正为实质 API」，替换原 resolveApi 的层 1-4 近似匹配。
#
#  本模块把原 resolveApi.py 的 `_name_of` / `_is_harmless` / `_forward_target` /
#  `_main_call_name` 移入复用（它们负责「提取名字链 + 识别 forward/nested」，
#  不涉及 import 近似），resolveApi.py 仅保留薄入口。

import ast
from typing import Dict, List, Optional, Set, Tuple

from Tool.model import ResolvedApi
from Tool.tool import SourceProvider


# ---------------------------------------------------------------------------
# 表达式名提取 / 转发与嵌套识别（自原 resolveApi.py 迁移，逻辑不变）
# ---------------------------------------------------------------------------

## 无害前置调用名（转发识别时忽略的 print / warnings 类语句）
_HARMLESS_CALLS = ('print', 'warnings.warn', 'warnings.warning', 'warn')


def _name_of(node: ast.AST) -> Optional[str]:
    """从表达式节点提取名字（Name -> 简单名；Attribute -> 点链；Call -> 被调 func）。

    输入参数：
        node (ast.AST)：表达式节点。
    返回值：
        Optional[str]：名字（简单名或点链）；无法提取返回 None。
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        return _name_of(node.func)
    return None


def _is_harmless(stmt: ast.stmt) -> bool:
    """判断语句是否无害前置（docstring / print / warnings.warn 等，不参与转发判定）。

    输入参数：
        stmt (ast.stmt)：函数体语句。
    返回值：
        bool：是无害前置语句返回 True。
    """
    if not isinstance(stmt, ast.Expr):
        return False
    val = stmt.value
    # docstring（字符串字面量）不参与转发判定
    if isinstance(val, ast.Constant) and isinstance(val.value, str):
        return True
    if not isinstance(val, ast.Call):
        return False
    return _name_of(val.func) in _HARMLESS_CALLS


def _forward_target(func: ast.AST) -> Optional[str]:
    """识别纯转发：函数体除无害前置（docstring/print/import）外只有一条
    return 单个调用/单个名字。

    函数体内局部 `from ._impl import x` 等 import 语句不产生 return、不改变
    控制流，仅引入名字，故不参与「是否纯转发」判定（名字由 ImportResolver
    的局部 import 表另行解析）。

    输入参数：
        func (ast.AST)：FunctionDef / AsyncFunctionDef 节点。
    返回值：
        Optional[str]：被调目标名；非纯转发返回 None。
    """
    body = [s for s in func.body
            if not _is_harmless(s)
            and not isinstance(s, (ast.Import, ast.ImportFrom))]
    if len(body) != 1:
        return None
    stmt = body[0]
    if not isinstance(stmt, ast.Return) or stmt.value is None:
        return None
    val = stmt.value
    if isinstance(val, ast.Call):
        return _name_of(val.func)
    if isinstance(val, ast.Name):
        return val.id
    return None


def _main_call_name(func: ast.AST) -> Optional[str]:
    """取函数体内按源码顺序第一个（非无害）调用的目标名。

    输入参数：
        func (ast.AST)：FunctionDef / AsyncFunctionDef 节点。
    返回值：
        Optional[str]：主调用目标名；无调用返回 None。
    """
    calls: List[Tuple[int, int, ast.Call]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            calls.append((node.lineno, node.col_offset, node))
    calls.sort(key=lambda t: (t[0], t[1]))
    for _, _, call in calls:
        name = _name_of(call.func)
        if name and name not in _HARMLESS_CALLS:
            return name
    return None


def _identify_call_target_node(func: ast.AST) -> Tuple[Optional[str], str]:
    """从 FunctionDef 节点识别调用目标，返回 (目标名, kind)。

    kind：'forward'（纯转发）| 'nested'（内含调用非转发）| 'direct'（无调用）。

    输入参数：
        func (ast.AST)：FunctionDef / AsyncFunctionDef 节点。
    返回值：
        Tuple[Optional[str], str]：(目标名或 None, kind)。
    """
    target = _forward_target(func)
    if target is not None:
        return target, 'forward'
    target = _main_call_name(func)
    if target is not None:
        return target, 'nested'
    return None, 'direct'


# ---------------------------------------------------------------------------
# 名字来源精确解析
# ---------------------------------------------------------------------------

class ImportResolver:
    """基于源码 AST 把名字精确解析为 internal_fqn，并递归定位实质 API 定义。

    依赖 SourceProvider 提供模块级 AST（module_ast / locate_module）。解析失败
    （库外 / 无法定位）一律返回 None，由调用方映射为 alias_external /
    nested_external（宁漏荐不错荐，见 resolve 流程）。
    """

    def __init__(self, provider: SourceProvider):
        """初始化。

        输入参数：
            provider (SourceProvider)：提供模块 AST / 模块定位能力。
        """
        self.provider = provider
        self.lib_name = provider.lib_name
        self._bindings_cache: Dict[Tuple[str, str], Dict[str, Tuple[str, str]]] = {}

    # ---- 名字绑定表 ----

    def _bindings(self, module_fqn: str, version: str) -> Dict[str, Tuple[str, str]]:
        """构建模块的名字绑定表：名字 -> (kind, source)。

        kind：'import'（导入源 FQN）| 'def' / 'class' / 'assign'（本模块 FQN）。
        按源码顺序后出现的绑定覆盖先出现的（Python 遮蔽语义）。

        输入参数：
            module_fqn (str)：模块 FQN。
            version (str)：版本号。
        返回值：
            Dict[str, Tuple[str, str]]：名字 -> (kind, source)。
        """
        key = (module_fqn, version)
        if key in self._bindings_cache:
            return self._bindings_cache[key]
        module_ast = self.provider.module_ast(module_fqn, version)
        bindings: Dict[str, Tuple[str, str]] = {}
        if module_ast is None:
            self._bindings_cache[key] = bindings
            return bindings
        for stmt in module_ast.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    name = alias.asname or alias.name.split('.')[0]
                    src = alias.name if alias.asname else alias.name.split('.')[0]
                    bindings[name] = ('import', src)
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    name = alias.asname or alias.name
                    src = self._resolve_import_from(stmt, module_fqn, alias.name)
                    bindings[name] = ('import', src)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bindings[stmt.name] = ('def', f"{module_fqn}.{stmt.name}")
            elif isinstance(stmt, ast.ClassDef):
                bindings[stmt.name] = ('class', f"{module_fqn}.{stmt.name}")
            elif isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name):
                        bindings[t.id] = ('assign', f"{module_fqn}.{t.id}")
        self._bindings_cache[key] = bindings
        return bindings

    def _resolve_import_from(self, node: ast.ImportFrom, module_fqn: str,
                             name: str) -> str:
        """解析 `from X import name` 的 X（含相对导入）为绝对导入源 FQN。

        输入参数：
            node (ast.ImportFrom)：ImportFrom 节点。
            module_fqn (str)：当前模块 FQN（用于相对导入层级解析）。
            name (str)：被导入的名字。
        返回值：
            str：绝对导入源 FQN（模块路径.名字）。
        """
        level = node.level
        module = node.module or ''
        if level == 0:
            base = module  # 绝对导入：module 即完整路径
        else:
            parts = module_fqn.split('.')
            base_parts = parts[:-level] if level <= len(parts) else []
            if module:
                base_parts = base_parts + module.split('.')
            base = '.'.join(base_parts)
        return f"{base}.{name}" if base else name

    # ---- 节点定位 ----

    def _find_in_body(self, body: List[ast.stmt], segs: List[str]) -> Optional[ast.AST]:
        """在 body 里按相对路径 segs 下钻定位节点。

        单段：定义（FunctionDef/ClassDef/Assign）优先，import 绑定兜底；
        多段：先定位 ClassDef 再递归下钻（方法 / 嵌套类）。

        输入参数：
            body (List[ast.stmt])：模块或类 body 语句列表。
            segs (List[str])：相对路径段（如 ['name'] 或 ['Class', 'method']）。
        返回值：
            Optional[ast.AST]：定位到的节点；找不到返回 None。
        """
        if not segs:
            return None
        head = segs[0]
        if len(segs) == 1:
            # 定义优先（真实定义 fqn 命中）
            for stmt in body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and stmt.name == head:
                    return stmt
            for stmt in body:
                if isinstance(stmt, ast.ClassDef) and stmt.name == head:
                    return stmt
            for stmt in body:
                if isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if isinstance(t, ast.Name) and t.id == head:
                            return stmt
            # import 中转兜底（名字在本模块仅 import 而来）
            for stmt in body:
                if isinstance(stmt, ast.Import):
                    for alias in stmt.names:
                        if (alias.asname or alias.name.split('.')[0]) == head:
                            return stmt
                elif isinstance(stmt, ast.ImportFrom):
                    for alias in stmt.names:
                        if (alias.asname or alias.name) == head:
                            return stmt
            return None
        # 多段：定位类再递归
        for stmt in body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == head:
                return self._find_in_body(stmt.body, segs[1:])
        return None

    def _locate(self, fqn: str, version: str) -> Optional[Tuple[ast.AST, str]]:
        """定位 internal_fqn 的 AST 节点，返回 (节点, 所属模块 FQN)。

        输入参数：
            fqn (str)：完整名。
            version (str)：版本号。
        返回值：
            Optional[Tuple[ast.AST, str]]：(节点, 模块 FQN)；找不到返回 None。
        """
        module_fqn = self.provider.locate_module(fqn, version)
        if module_fqn is None:
            return None
        module_ast = self.provider.module_ast(module_fqn, version)
        if module_ast is None:
            return None
        rel = fqn[len(module_fqn):].lstrip('.')
        if not rel:
            return None
        node = self._find_in_body(module_ast.body, rel.split('.'))
        if node is None:
            return None
        return node, module_fqn

    # ---- 名字解析 ----

    def _descend(self, current_fqn: str, seg: str, version: str) -> Optional[str]:
        """点链下钻：在 current_fqn 指向的模块/对象上解析 seg 一段。

        输入参数：
            current_fqn (str)：当前已解析到的 FQN（模块或对象）。
            seg (str)：点链的下一段名字。
            version (str)：版本号。
        返回值：
            Optional[str]：下钻后的 FQN；失败返回 None。
        """
        # 1) 当前是模块：在绑定表查对象，或尝试子模块
        if self.provider.module_ast(current_fqn, version) is not None:
            bindings = self._bindings(current_fqn, version)
            if seg in bindings:
                return bindings[seg][1]
            sub = f"{current_fqn}.{seg}"
            if self.provider.module_ast(sub, version) is not None:
                return sub
            return None
        # 2) 当前是类：查类成员方法
        located = self._locate(current_fqn, version)
        if located is not None:
            node, _ = located
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and sub.name == seg:
                        return f"{current_fqn}.{seg}"
        return None

    def _local_imports(self, func: ast.AST, module_fqn: str) -> Dict[str, str]:
        """收集函数体顶层 import 语句的局部绑定：名字 -> 导入源 FQN。

        模块级 `_bindings` 只覆盖模块顶层 import，不覆盖函数作用域内的局部
        `from ._impl import x`；本方法补齐后者，供函数体内 import 后调用
        `x(...)` 的精确解析。

        输入参数：
            func (ast.AST)：FunctionDef / AsyncFunctionDef 节点。
            module_fqn (str)：函数所在模块 FQN。
        返回值：
            Dict[str, str]：局部 import 名字 -> 导入源 FQN。
        """
        local: Dict[str, str] = {}
        for stmt in func.body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    name = alias.asname or alias.name.split('.')[0]
                    local[name] = alias.name
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    name = alias.asname or alias.name
                    local[name] = self._resolve_import_from(stmt, module_fqn, alias.name)
        return local

    def _resolve_name(self, name: str, module_fqn: str, version: str,
                      local_imports: Optional[Dict[str, str]] = None) -> Optional[str]:
        """把名字链（简单名或点链）精确解析为 internal_fqn。

        根名字先查函数局部 import（若有），再查模块绑定表（import 表 ∪ 定义表），
        点链逐段下钻；根名字两处都不在时，若 name 本身是完整 FQN（绝对导入调用，
        如 `fakelib.mod.new_api()`）且可定位到定义节点，则直接返回该 FQN；
        否则（内置 / 标准库 / 第三方 / 未定义）或下钻失败返回 None。

        输入参数：
            name (str)：名字链（如 'new_api'、'_impl.new_api'）。
            module_fqn (str)：名字出现的模块 FQN。
            version (str)：版本号。
            local_imports (Optional[Dict[str, str]])：函数体局部 import 绑定。
        返回值：
            Optional[str]：target internal_fqn；解析失败（库外）返回 None。
        """
        parts = name.split('.')
        root = parts[0]
        # 1) 函数局部 import 优先
        if local_imports and root in local_imports:
            source = local_imports[root]
        else:
            bindings = self._bindings(module_fqn, version)
            if root not in bindings:
                # 完整 FQN 兜底：name 本身是 internal_fqn（绝对导入调用，
                # 如 fakelib.mod.new_api()），直接验证是否可定位到定义节点
                if '.' in name and self._locate(name, version) is not None:
                    return name
                return None  # 根名字非本模块 import/定义 -> 库外
            source = bindings[root][1]
        if len(parts) == 1:
            return source
        current = source
        for seg in parts[1:]:
            current = self._descend(current, seg, version)
            if current is None:
                return None
        return current

    def _self_target(self, name: str, fqn: str) -> str:
        """self.xxx / cls.xxx -> 所在类同名方法 FQN（精确，非近似）。

        输入参数：
            name (str)：'self.attr' 或 'cls.attr'。
            fqn (str)：当前方法 internal_fqn（类.方法）。
        返回值：
            str：目标 FQN（类.attr）。
        """
        attr = name.split('.', 1)[1]
        cls_prefix = fqn.rsplit('.', 1)[0]
        return f"{cls_prefix}.{attr}"

    def _resolve_target(self, name: str, module_fqn: str, version: str,
                        fqn: str, api_type: str,
                        local_imports: Optional[Dict[str, str]] = None) -> Optional[str]:
        """统一解析调用/赋值目标名 -> target internal_fqn（含 self/cls）。

        输入参数：
            name (str)：目标名字链。
            module_fqn (str)：出现模块 FQN。
            version (str)：版本号。
            fqn (str)：当前 API 完整名（self/cls 解析需定位所在类）。
            api_type (str)：'class' | 'function' | 'method'。
            local_imports (Optional[Dict[str, str]])：函数体局部 import 绑定。
        返回值：
            Optional[str]：target FQN；解析失败返回 None。
        """
        if name.startswith('self.') or name.startswith('cls.'):
            if api_type != 'method':
                return None
            return self._self_target(name, fqn)
        return self._resolve_name(name, module_fqn, version, local_imports)

    # ---- 主入口 ----

    def resolve(self, fqn: str, version: str, api_type: str,
                visited: Optional[Set[str]] = None) -> ResolvedApi:
        """把 fqn 更正为实质 API 定义（赋值/嵌套调用/import 链递归 + visited 防环）。

        流程对应设计：定位节点 -> 赋值形态追 value / 定义形态判断嵌套调用 ->
        递归解析目标直到 FunctionDef/ClassDef（定义）或库外（空推荐）。

        输入参数：
            fqn (str)：待分析 API 完整名。
            version (str)：fqn 所在版本。
            api_type (str)：'class' | 'function' | 'method'。
            visited (Optional[Set[str]])：递归防环集合，外部无需传入。
        返回值：
            ResolvedApi：kind 语义见 model.ResolvedApi；original_fqn 恒为最外层输入。
        """
        if visited is None:
            visited = set()
        if fqn in visited:  # 防环
            return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn, kind='unknown')
        visited.add(fqn)

        located = self._locate(fqn, version)
        if located is None:
            return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn,
                               kind='unknown', definition=None)
        node, module_fqn = located

        # ---- 赋值别名形态 ----
        if isinstance(node, ast.Assign):
            name = _name_of(node.value)
            if name is None:
                return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn,
                                   kind='alias_external')
            target = self._resolve_target(name, module_fqn, version, fqn, api_type)
            if target is None:
                return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn,
                                   kind='alias_external')
            inner = self.resolve(target, version, api_type, visited)
            if inner.kind in ('alias_external', 'nested_external', 'unknown'):
                return ResolvedApi(original_fqn=fqn, resolved_fqn=inner.resolved_fqn,
                                   kind=inner.kind, definition=inner.definition)
            return ResolvedApi(original_fqn=fqn, resolved_fqn=inner.resolved_fqn,
                               kind='alias', definition=inner.definition)

        # ---- import 中转形态（递归链中目标仍是 import 来的）----
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            name = fqn.rsplit('.', 1)[-1]
            bindings = self._bindings(module_fqn, version)
            entry = bindings.get(name)
            if entry is not None and entry[0] == 'import':
                inner = self.resolve(entry[1], version, api_type, visited)
                if inner.kind in ('alias_external', 'nested_external', 'unknown'):
                    return ResolvedApi(original_fqn=fqn, resolved_fqn=inner.resolved_fqn,
                                       kind=inner.kind, definition=inner.definition)
                return ResolvedApi(original_fqn=fqn, resolved_fqn=inner.resolved_fqn,
                                   kind='alias', definition=inner.definition)
            return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn,
                               kind='unknown', definition=None)

        # ---- 定义形态：class 无嵌套语义 ----
        if isinstance(node, ast.ClassDef) or api_type == 'class':
            return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn,
                               kind='direct', definition=ast.unparse(node))

        # ---- 定义形态：函数 -> 判断嵌套调用 ----
        target, kind = _identify_call_target_node(node)
        if target is None:
            return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn,
                               kind='direct', definition=ast.unparse(node))
        local_imports = self._local_imports(node, module_fqn)
        resolved_target = self._resolve_target(target, module_fqn, version, fqn,
                                               api_type, local_imports)
        if resolved_target is None:
            return ResolvedApi(original_fqn=fqn, resolved_fqn=fqn,
                               kind='nested_external', definition=ast.unparse(node))
        inner = self.resolve(resolved_target, version, api_type, visited)
        if inner.kind in ('alias_external', 'nested_external', 'unknown'):
            return ResolvedApi(original_fqn=fqn, resolved_fqn=inner.resolved_fqn,
                               kind=inner.kind, definition=inner.definition)
        return ResolvedApi(original_fqn=fqn, resolved_fqn=inner.resolved_fqn,
                           kind=kind, definition=inner.definition)
