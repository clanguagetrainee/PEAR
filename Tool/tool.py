## @package tool
#  通用工具
#  职责：提供跨模块共享的基础能力——
#   1) load_task：Configure JSON / CLI 覆盖 → Task；
#   2) load_knowledge_base：LibAPIExtraction 文本产物 → 内存 KnowledgeBase；
#   3) SourceProvider：按 (库,粒度,版本) 整批生成完整 API 定义并持久缓存，
#      内部管理 git worktree 生命周期。
#  依赖方向：Tool → Knowledge（复用 getSource / getVersion / py2parse / Def2format），
#  不反向。对应设计文档 §6 Tool 定位与 §9.6 body 按需提取。

import ast
import fcntl
import json
import os
import re
from typing import Dict, List, Optional, Tuple

from Tool.model import APIRecord, KnowledgeBase, Task
from Knowledge.getSource import checkout_version, remove_worktree, worktree_path
from Knowledge.getVersion import list_versions
from Knowledge._pcart.py2parse import safe_parse
from Knowledge._pcart.extractDef import Def2format


## 合法 API 粒度
_API_TYPES = ('class', 'function', 'method')


def _version_key(version: str) -> Tuple[int, ...]:
    """版本号字符串 → 可比较数字元组。

    输入参数：
        version (str)：纯数字版本号，如 "2.0.0"。
    返回值：
        Tuple[int, ...]：各数字段组成的元组。
    """
    return tuple(int(part) for part in version.split('.'))


# ---------------------------------------------------------------------------
# 1) 配置加载
# ---------------------------------------------------------------------------

## camelCase 配置字段 → Task snake_case 字段的映射
_FIELD_MAP = {
    'libName': 'lib_name',
    'sourceVersion': 'source_version',
    'targetVersion': 'target_version',
    'oldApiFqn': 'old_api_fqn',
    'apiType': 'api_type',
    'topK': 'top_k',
    'libRepoPath': 'lib_repo_path',
}


def load_task(config_path: str, overrides: Optional[dict] = None) -> Task:
    """加载 Configure JSON 并应用 CLI 覆盖，得到 Task。

    输入参数：
        config_path (str)：Configure JSON 文件路径。
        overrides (Optional[dict])：CLI 覆盖字段（snake_case key，如
            {'top_k': 5, 'source_version': '1.0.0'}），None 表示不覆盖。
    返回值：
        Task：统一任务模型。
    异常：
        FileNotFoundError：配置文件不存在。
        ValueError：缺必填字段 / api_type 非法。
    """
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with open(config_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    data: Dict[str, object] = {}
    for camel, snake in _FIELD_MAP.items():
        if camel in raw:
            data[snake] = raw[camel]

    if overrides:
        for k, v in overrides.items():
            if k in data or k in _FIELD_MAP.values():
                data[k] = v

    required = ['lib_name', 'source_version', 'target_version',
                'old_api_fqn', 'api_type', 'lib_repo_path']
    missing = [k for k in required if k not in data or data[k] in (None, '')]
    if missing:
        raise ValueError(f"配置缺必填字段: {', '.join(missing)}")

    api_type = data['api_type']
    if api_type not in _API_TYPES:
        raise ValueError(f"api_type 非法: {api_type!r}（应为 {'/'.join(_API_TYPES)}）")

    top_k = int(data.get('top_k', 3))
    if top_k <= 0:
        raise ValueError(f"topK 必须为正整数，当前 {top_k}")

    return Task(
        lib_name=data['lib_name'],
        source_version=data['source_version'],
        target_version=data['target_version'],
        old_api_fqn=data['old_api_fqn'],
        api_type=api_type,
        top_k=top_k,
        lib_repo_path=data['lib_repo_path'],
    )


# ---------------------------------------------------------------------------
# 2) 知识库加载
# ---------------------------------------------------------------------------

## 知识库文件名中的版本号正则（{libName}{version} 或 {version}.json）
def _extract_version(fname: str, lib_name: str) -> Optional[str]:
    """从知识库文件名提取规范化版本号。

    输入参数：
        fname (str)：文件名（如 pandas0.19.0 或 0.19.0.json）。
        lib_name (str)：库名。
    返回值：
        Optional[str]：版本号；文件名不符合命名约定返回 None。
    """
    m = re.fullmatch(rf'{re.escape(lib_name)}(\d+\.\d+(?:\.\d+)*)', fname)
    if m:
        return m.group(1)
    m = re.fullmatch(r'(\d+\.\d+(?:\.\d+)*)\.json', fname)
    if m:
        return m.group(1)
    return None


def _parse_record_line(line: str) -> Optional[APIRecord]:
    """将知识库单行解析为 APIRecord；忽略分节标记行与空行。

    输入参数：
        line (str)：知识库文件的一行（已 strip）。
    返回值：
        Optional[APIRecord]：赋值行 / 定义行解析结果；分节标记或空行返回 None。
    """
    if not line or line.startswith('-'):
        return None
    if line.startswith('A:'):
        body = line[2:]
        idx = body.find('->')
        if idx < 0:
            return None
        fqn = body[:idx].strip()
        value = body[idx + 2:].strip()
        return APIRecord(fqn=fqn, signature='', alias_of=value)
    # 定义行：fqn(args)->ret 或 fqn(Base)；第一个 '(' 前为 fqn
    idx = line.find('(')
    if idx < 0:
        return None
    fqn = line[:idx].strip()
    signature = line[idx:].strip()
    return APIRecord(fqn=fqn, signature=signature, alias_of=None)


def load_knowledge_base(lib_name: str, kb_dir: str) -> KnowledgeBase:
    """把 LibAPIExtraction/{lib_name}/ 下的版本文件解析为内存 KnowledgeBase。

    输入参数：
        lib_name (str)：库名。
        kb_dir (str)：知识库根目录（LibAPIExtraction）。
    返回值：
        KnowledgeBase：versions 升序；_records[version] = {fqn: APIRecord}。
    异常：
        FileNotFoundError：知识库目录不存在。
        ValueError：目录下无任何版本文件。
    """
    lib_dir = os.path.join(kb_dir, lib_name)
    if not os.path.isdir(lib_dir):
        raise FileNotFoundError(f"知识库目录不存在: {lib_dir}")

    kb = KnowledgeBase(lib_name=lib_name)
    versions: List[str] = []
    for fname in sorted(os.listdir(lib_dir)):
        version = _extract_version(fname, lib_name)
        if version is None:
            continue
        path = os.path.join(lib_dir, fname)
        if not os.path.isfile(path):
            continue
        records: Dict[str, APIRecord] = {}
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                rec = _parse_record_line(line.strip())
                if rec is not None:
                    # 同名 fqn 后出现的行覆盖前者（赋值行 vs 定义行取后出现的）
                    records[rec.fqn] = rec
        kb._records[version] = records
        versions.append(version)

    if not versions:
        raise ValueError(f"{lib_dir} 下无任何版本知识库文件，请先执行 build")
    kb.versions = sorted(versions, key=_version_key)
    return kb


# ---------------------------------------------------------------------------
# 3) SourceProvider —— 完整 API 定义整批生成 + 持久缓存
# ---------------------------------------------------------------------------

class SourceProvider:
    """按 (库,粒度,版本) 整批生成完整 API 定义 + 持久缓存；内部管理 git worktree。

    持久缓存结构：{cache_dir}/{lib}/{type}/{version}/{internal_fqn}.py
    - key 用 internal_fqn（真实源码定义处），与知识库定义行 fqn 对齐；
    - 生成单位是「库 × 粒度 × 版本」整批，整批完成后写 {version}/.done；
    - 不做预提取，仅在 get_api 首次命中该批时按需整批生成。
    """

    def __init__(self, lib_name: str, lib_repo_path: str, cache_dir: str,
                 worktrees_root: Optional[str] = None):
        """初始化 SourceProvider。

        输入参数：
            lib_name (str)：库名。
            lib_repo_path (str)：本地 git 仓库路径（libRepoPath）。
            cache_dir (str)：定义缓存根目录（CodeCache）。
            worktrees_root (Optional[str])：worktree 根目录，None 用默认。
        """
        self.lib_name = lib_name
        self.lib_repo_path = lib_repo_path
        self.cache_dir = cache_dir
        self.worktrees_root = worktrees_root
        self._version_tags: Optional[Dict[str, str]] = None  # version -> tag，惰性加载
        self._worktree_dirs: Dict[str, str] = {}             # version -> src 目录
        self._module_index: Dict[str, Dict[str, str]] = {}   # version -> {module_fqn: file_path}
        self._module_ast_cache: Dict[str, Dict[str, ast.Module]] = {}  # version -> {module_fqn: ast}

    # ---- 内部：版本 tag 映射 ----

    def _get_version_tags(self) -> Dict[str, str]:
        """惰性加载 (version -> tag) 映射，进程内复用。

        返回值：
            Dict[str, str]：规范化版本号 -> 原始 git tag。
        异常：
            FileNotFoundError：仓库路径不存在。
        """
        if self._version_tags is None:
            pairs = list_versions(self.lib_repo_path)
            self._version_tags = {v: t for v, t in pairs}
        return self._version_tags

    # ---- 内部：包根探测（与 build.py 一致）----

    def _resolve_package_root(self, src_dir: str) -> str:
        """探测库包目录（遍历范围）。

        优先同名包目录（pandas/django），其次 lib/{lib_name} 布局（matplotlib），
        否则退回源码根。

        输入参数：
            src_dir (str)：worktree 源码根目录。
        返回值：
            str：库包目录路径。
        """
        for cand in (os.path.join(src_dir, self.lib_name),
                     os.path.join(src_dir, 'lib', self.lib_name)):
            if os.path.isdir(cand):
                return cand
        return src_dir

    # ---- 内部：遍历源文件 ----

    def _iter_source_files(self, pkg_root: str) -> List[Tuple[str, bool]]:
        """收集包目录下 .py / .pyi 源文件，返回 (路径, 是否 pyi)。

        .pyi 仅在其对应 .py 不存在时纳入（.py 为权威定义，.pyi 只补纯 stub）。
        过滤隐藏文件 / __pycache__ / include 目录。

        输入参数：
            pkg_root (str)：库包目录。
        返回值：
            List[Tuple[str, bool]]：(文件绝对路径, 是否 .pyi)。
        """
        py_files: List[str] = []
        pyi_files: List[str] = []
        for root, dirs, files in os.walk(pkg_root):
            dirs[:] = [d for d in dirs
                       if not d.startswith('.') and d not in ('__pycache__', 'include')]
            for f in files:
                if f.startswith('.'):
                    continue
                if f.endswith('.py'):
                    py_files.append(os.path.join(root, f))
                elif f.endswith('.pyi'):
                    pyi_files.append(os.path.join(root, f))
        py_set = set(py_files)
        result = [(p, False) for p in py_files]
        for p in pyi_files:
            if p[:-1] not in py_set:  # 对应 .py 不存在时才纳入 .pyi
                result.append((p, True))
        return result

    # ---- 内部：AST 收集（fqn 构造与知识库 getDef 对齐）----

    @staticmethod
    def _skip_overload(node: ast.AST, pyi: bool) -> bool:
        """判断节点是否应跳过（非 pyi 文件里带 overload 装饰器的跳过）。

        输入参数：
            node (ast.AST)：FunctionDef/AsyncFunctionDef 节点。
            pyi (bool)：是否 .pyi 文件（.pyi 不跳 overload）。
        返回值：
            bool：应跳过返回 True。
        """
        if pyi:
            return False
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        return 'overload' in ast.unparse(node.decorator_list)

    @classmethod
    def _collect(cls, module_body: List[ast.stmt], prefix: str, api_type: str,
                 pyi: bool, out: Dict[str, str]) -> None:
        """遍历模块 body，按粒度收集 {internal_fqn: 完整定义文本}。

        fqn 构造规则与知识库 getDef 的 getClass/task 对齐：
        - class 粒度：{prefix}.{ClassName}（含嵌套类 {prefix}.{Outer}.{Inner}）
        - function 粒度：{prefix}.{funcName}（模块级函数）
        - method 粒度：{prefix}.{ClassName}.{methodName}（类内方法，排除
          __init__/__new__/__call__，与 getClass 一致）

        输入参数：
            module_body (List[ast.stmt])：模块级语句列表。
            prefix (str)：模块 FQN 前缀（包名.文件名，Def2format.prefix）。
            api_type (str)：'class' | 'function' | 'method'。
            pyi (bool)：是否 .pyi 文件。
            out (Dict[str, str])：收集结果 {fqn: ast.unparse 整节点文本}。
        """
        for node in module_body:
            if isinstance(node, ast.ClassDef):
                cls._collect_class(node, prefix, api_type, pyi, out)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if api_type == 'function' and not cls._skip_overload(node, pyi):
                    out[f"{prefix}.{node.name}"] = ast.unparse(node)

    @classmethod
    def _collect_class(cls, cls_node: ast.ClassDef, prefix: str, api_type: str,
                       pyi: bool, out: Dict[str, str]) -> None:
        """收集类节点（含类本身 / 类内方法 / 嵌套类）。

        输入参数：
            cls_node (ast.ClassDef)：类节点。
            prefix (str)：类所在模块的 FQN 前缀。
            api_type (str)：'class' | 'function' | 'method'。
            pyi (bool)：是否 .pyi 文件。
            out (Dict[str, str])：收集结果。
        """
        cls_fqn = f"{prefix}.{cls_node.name}"
        if api_type == 'class':
            out[cls_fqn] = ast.unparse(cls_node)
        if api_type == 'method':
            for sub in ast.iter_child_nodes(cls_node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name in ('__init__', '__new__', '__call__'):
                        continue
                    if cls._skip_overload(sub, pyi):
                        continue
                    out[f"{cls_fqn}.{sub.name}"] = ast.unparse(sub)
        # 嵌套类递归
        for sub in ast.iter_child_nodes(cls_node):
            if isinstance(sub, ast.ClassDef):
                cls._collect_class(sub, cls_fqn, api_type, pyi, out)

    # ---- 对外：批生成 / 读取 ----

    def _batch_dir(self, api_type: str, version: str) -> str:
        """返回某 (粒度,版本) 的缓存目录。

        输入参数：
            api_type (str)：'class' | 'function' | 'method'。
            version (str)：版本号。
        返回值：
            str：缓存目录绝对路径。
        """
        return os.path.join(self.cache_dir, self.lib_name, api_type, version)

    def source_dir(self, version: str) -> str:
        """返回 version 对应源码目录（worktree，进程内复用）。

        输入参数：
            version (str)：版本号。
        返回值：
            str：worktree 源码根目录。
        异常：
            ValueError：version 无对应 tag（不在仓库版本序列中）。
        """
        if version in self._worktree_dirs:
            return self._worktree_dirs[version]
        tags = self._get_version_tags()
        if version not in tags:
            raise ValueError(f"版本 {version} 不在仓库版本序列中，无法切出源码")
        dest = worktree_path(self.lib_name, version, self.worktrees_root)
        src = checkout_version(self.lib_repo_path, tags[version], dest)
        self._worktree_dirs[version] = src
        return src

    # ---- 模块级 AST 访问（供 ImportResolver 精确解析 import/定义）----

    def _get_module_index(self, version: str) -> Dict[str, str]:
        """构建 version 的 module_fqn -> 文件路径 索引（惰性，进程内缓存）。

        输入参数：
            version (str)：版本号。
        返回值：
            Dict[str, str]：{模块 FQN 前缀: 源码文件绝对路径}。
        """
        if version in self._module_index:
            return self._module_index[version]
        src_dir = self.source_dir(version)
        pkg_root = self._resolve_package_root(src_dir)
        index: Dict[str, str] = {}
        for fpath, pyi in self._iter_source_files(pkg_root):
            d2f = Def2format()
            d2f.toFormat(fpath, src_dir, pkg_root, self.lib_name)
            prefix = d2f.prefix
            if prefix not in index or not pyi:  # .py 优先于 .pyi
                index[prefix] = fpath
        self._module_index[version] = index
        return index

    def locate_module(self, fqn: str, version: str) -> Optional[str]:
        """返回 fqn 所属模块的 FQN（对模块索引做最长前缀匹配）。

        输入参数：
            fqn (str)：完整名（internal_fqn）。
            version (str)：版本号。
        返回值：
            Optional[str]：模块 FQN；无匹配返回 None。
        """
        index = self._get_module_index(version)
        parts = fqn.split('.')
        for i in range(len(parts), 0, -1):
            prefix = '.'.join(parts[:i])
            if prefix in index:
                return prefix
        return None

    def module_ast(self, module_fqn: str, version: str) -> Optional[ast.Module]:
        """返回 module_fqn 对应模块文件在 version 的 AST 根（惰性解析 + 缓存）。

        输入参数：
            module_fqn (str)：模块 FQN（如 fakelib._impl）。
            version (str)：版本号。
        返回值：
            Optional[ast.Module]：AST 根；模块不存在或解析失败返回 None。
        """
        cache = self._module_ast_cache.setdefault(version, {})
        if module_fqn in cache:
            return cache[module_fqn]
        index = self._get_module_index(version)
        if module_fqn not in index:
            return None
        fpath = index[module_fqn]
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                code = f.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"[SourceProvider] {fpath} 读取失败: {e}")
            return None
        tree = safe_parse(code, fpath)
        cache[module_fqn] = tree
        return tree

    def _acquire_version_lock(self, version: str):
        """获取 (lib, version) 级别的进程间互斥锁（fcntl.flock，阻塞等待）。

        锁粒度取 (lib, version) 而非 (lib, type, version)：同一版本的
        class/function/method 批生成共享同一个 git worktree 检出，必须串行。
        锁文件常驻 {cache_dir}/.locks/{lib}/{version}.lock，永不删除——
        删除会让后来的进程在新路径上新建锁文件，与旧 fd 的锁不是同一把，
        破坏互斥。解锁由调用方 finally 中 flock(LOCK_UN) + close 完成，
        进程异常退出时内核自动释放。

        输入参数：
            version (str)：规范化版本号。
        返回值：
            IO：已持有 LOCK_EX 的锁文件句柄（调用方负责 finally 释放）。
        """
        lock_dir = os.path.join(self.cache_dir, '.locks', self.lib_name)
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, f'{version}.lock')
        fh = open(lock_path, 'a+')
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def ensure_batch(self, api_type: str, version: str) -> None:
        """确保 (lib, api_type, version) 批已生成；已生成则无操作，否则整批提取落盘。

        并发安全：快路径（.done 已存在）不加锁直接返回；缺失时按 (lib, version)
        加进程间互斥锁，锁内二次检查 .done——并发中其他进程已生成则直接复用其
        产物，只有首个持锁且仍未生成者才执行提取（不会重复生成、不会覆盖）。

        输入参数：
            api_type (str)：'class' | 'function' | 'method'。
            version (str)：版本号。
        异常：
            ValueError：api_type 非法 / version 无对应 tag。
            OSError：目录创建或文件写入失败。
        """
        if api_type not in _API_TYPES:
            raise ValueError(f"api_type 非法: {api_type!r}")
        batch_dir = self._batch_dir(api_type, version)
        done = os.path.join(batch_dir, '.done')
        if os.path.exists(done):
            return  # 快路径：已有批次，不加锁

        # 慢路径：缺失批次，加锁后二次检查再生成
        fh = self._acquire_version_lock(version)
        try:
            if os.path.exists(done):
                return  # 二次检查：并发中其他进程已生成完毕，直接复用

            src_dir = self.source_dir(version)
            pkg_root = self._resolve_package_root(src_dir)

            defs: Dict[str, str] = {}
            for fpath, pyi in self._iter_source_files(pkg_root):
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        code = f.read()
                except (OSError, UnicodeDecodeError) as e:
                    print(f"[SourceProvider] {fpath} 读取失败: {e}")
                    continue
                tree = safe_parse(code, fpath)
                if tree is None:
                    continue
                d2f = Def2format()
                d2f.toFormat(fpath, src_dir, pkg_root, self.lib_name)
                prefix = d2f.prefix
                self._collect(tree.body, prefix, api_type, pyi, defs)

            os.makedirs(batch_dir, exist_ok=True)
            for fqn, text in defs.items():
                # internal_fqn 只含点号与标识符，不含路径分隔符，可直接作文件名
                out_path = os.path.join(batch_dir, f"{fqn}.py")
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(text)
            with open(done, 'w', encoding='utf-8') as f:
                f.write('')
            print(f"[SourceProvider] 生成 {self.lib_name} {version} {api_type} "
                  f"定义 {len(defs)} 个 -> {batch_dir}")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def get_api(self, internal_fqn: str, api_type: str, version: str) -> Optional[str]:
        """返回 internal_fqn 在 version 的完整 API 定义文本；该批未生成则整批生成。

        输入参数：
            internal_fqn (str)：真实源码定义处 FQN。
            api_type (str)：'class' | 'function' | 'method'，决定粒度目录。
            version (str)：版本号。
        返回值：
            Optional[str]：完整定义文本（装饰器+签名+实现 ast.unparse）；
                提取失败 / 源码无此定义返回 None。
        """
        self.ensure_batch(api_type, version)
        path = os.path.join(self._batch_dir(api_type, version), f"{internal_fqn}.py")
        if not os.path.isfile(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def list_api(self, api_type: str, version: str) -> List[str]:
        """返回该 (粒度,版本) 批的全部 internal_fqn（供 recommend 候选全集）。

        输入参数：
            api_type (str)：'class' | 'function' | 'method'。
            version (str)：版本号。
        返回值：
            List[str]：internal_fqn 列表（排序后）。
        """
        self.ensure_batch(api_type, version)
        batch_dir = self._batch_dir(api_type, version)
        if not os.path.isdir(batch_dir):
            return []
        return sorted(f[:-3] for f in os.listdir(batch_dir) if f.endswith('.py'))

    def close(self) -> None:
        """清理全部 worktree（Pipeline 结束调用）；缓存文件与 .done 保留。

        清理失败逐项打印，不抛出（不掩盖主流程结果）。
        """
        for version, dest in self._worktree_dirs.items():
            try:
                remove_worktree(self.lib_repo_path, dest)
            except Exception as e:
                print(f"[SourceProvider] worktree 清理失败 {version} ({dest}): {e}")
        self._worktree_dirs.clear()
