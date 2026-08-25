# PEAR 模块接口规范（interface-first）

> **日期**：2026-08-24
> **状态**：接口草案（待逐模块 review 确认，确认后按此实现）
> **用途**：在编写具体实现前，把主流程各模块的接口（签名 + docstring + 伪代码）
> 与数据流显式化，功能取舍在编码前落地。
> 关联文档：[design.md](./design.md)（§6 模块划分、§8 处理流程为本文依据）。

---

## 0. 数据流总览

```
main.py recommend
   │ 1. 检查 [Vs, Vt] 知识库缺失 → 缺失报错
   │ 2. load_knowledge_base(lib, kb_dir) → KnowledgeBase（内存）
   │ 3. run_pipeline(task, kb) → Result
   └─ 格式化输出候选列表

Pipeline.run_pipeline  ← 有状态 BFS 迭代编排（唯一持有 visited / worktree 生命周期）
   │ 对每个分支 fqn@pos 循环：
   │   Adjust.get_boundary  fqn 失效相邻版本对 (Vpre, Vpost)     ← 只查 kb
   │   Adjust.resolve_api   调整 A → 实质 API B（别名查 kb / 转发嵌套查定义，递归更正）
   │   Recommend.recommend  Vpost 全库同类型 Top-k，以 B 的完整定义比对   ← 查 kb + provider
   │   候选 ∈ Vt → 有效候选；否则 visited 检查后以 (candidate, vpost) 继续
   └─ 汇总：合并去重 → 路径评分(∏) → 排序

依赖方向（单向，不回头）：
   main → Pipeline → Adjust, Recommend → Knowledge
                                   ↑
                        Tool(model/tool) 被所有模块引用
```

**三个关键传递对象**（Tool/model.py，被所有模块 import）：
- `KnowledgeBase` —— 存在性判定 / 签名（只读知识库；type 由 Configure 提供，不进 kb）
- `SourceProvider` —— 按 (库,粒度,版本) 整批生成完整 API 定义 + 持久缓存（管理 worktree 生命周期）
- `Task` / `Candidate` / `BoundaryResult` / `ResolvedApi` / `Result` —— 各阶段数据载体

---

## 1. Tool/model.py —— 数据模型

### 定位
定义全流程共享的数据结构（dataclass），不包含任何逻辑，被所有模块引用。

### 接口

```python
@dataclass
class Task:
    """单个分析任务（Configure JSON / CLI 覆盖的统一内部表示）。
    字段：lib_name, source_version, target_version, old_api_fqn, api_type, top_k, lib_repo_path
        api_type 为用户手动提供的 old_api_fqn 类型（'class'|'function'|'method'），
        全程用于同类型比对；kb 不推断 type。
    """

@dataclass
class APIRecord:
    """知识库单条 API 记录。

    当前阶段（不做公开名识别）：全链路用完整名 internal_fqn，fqn 即完整名；
    type 不进 kb（由 Configure 的 api_type 提供）；alias_of 仅赋值行解析得到，
    当前 resolve_api 不使用，保留待公开名识别阶段。
    fqn: str            # 完整名（internal_fqn，源码真实定义路径）
    signature: str      # '(self, other, ignore_index=False)->None'（定义行；赋值行为空）
    alias_of: Optional[str]  # 仅赋值行（A:）存在，指向赋值目标
    """

@dataclass
class KnowledgeBase:
    """某库全部版本的内存化知识库（只读）。
    lib_name: str
    versions: List[str]                    # 升序版本号
    _records: Dict[str, Dict[str, APIRecord]]  # version -> {fqn: record}

    def exists(self, fqn: str, version: str) -> bool
    def get(self, fqn: str, version: str) -> Optional[APIRecord]
    def all_records(self, version: str) -> Dict[str, APIRecord]
    """

@dataclass
class BoundaryResult:
    """get_boundary 的返回：失效边界三态。
    status: str          # 'DEPRECATED' | 'NOT_DEPRECATED' | 'NOT_FOUND'
    vpre: Optional[str]  # status=DEPRECATED 时：最后一次存在的版本
    vpost: Optional[str] # status=DEPRECATED 时：第一次不存在的版本
    """

@dataclass
class ResolvedApi:
    """resolve_api 的返回：待分析 API 更正到实质 API 后的结果。
    original_fqn: str       # 输入的 API 完整名
    resolved_fqn: str       # 更正后的实质 API FQN（用于相似度比对）
    kind: str               # 'direct' | 'alias' | 'forward' | 'nested'
                           # | 'alias_external' | 'nested_external' | 'unknown'
    definition: Optional[str]  # resolved_fqn 在该版本的完整 API 定义
                               # （装饰器+签名+实现，ast.unparse 整节点），无则 None
    """

@dataclass
class Candidate:
    """单跳推荐结果（Pipeline 汇总前的中间形态）。
    fqn: str                # 候选 FQN
    api_type: str           # 与源同 type
    similarity: float       # 本跳 token 相似度 [0,1]
    evolution_path: List[str]  # 到本候选为止的演化路径（含本候选）
    local_scores: List[float]  # 路径上每一跳的相似度（与 path 对齐）
    """

@dataclass
class Result:
    """最终输出（run_pipeline 的返回）。
    task: Task
    status: str          # 'OK' | 'NOT_DEPRECATED' | 'NOT_FOUND' | 'NO_CANDIDATE' | 'ERROR'
    candidates: List[Candidate]  # 已按最终评分降序，去重后
    error: Optional[str] # status=ERROR 时的错误描述
    """
```

### 待确认点
- 无（纯数据结构，随各模块确认微调字段）。

---

## 2. Tool/tool.py —— 通用工具

### 定位
三个能力：配置加载（Configure/CLI → Task）、知识库加载（磁盘文本 → 内存 KnowledgeBase）、按 (库,粒度,版本) 整批提取完整 API 定义（SourceProvider，管理 worktree + 持久缓存生命周期）。

### 接口

```python
def load_task(config_path: str, overrides: Optional[dict] = None) -> Task:
    """加载 Configure JSON 并应用 CLI 覆盖，得到 Task。
    异常：FileNotFoundError（配置不存在）；ValueError（缺必填字段 / 版本号不合法）。
    """

def load_knowledge_base(lib_name: str, kb_dir: str) -> KnowledgeBase:
    """把 LibAPIExtraction/{lib_name}/ 下的版本文件解析为内存 KnowledgeBase。
    输入：lib_name；kb_dir（知识库根目录）。
    返回：KnowledgeBase（versions 升序；无任何版本文件时抛 ValueError）。
    # 关键决策：直接解析现有 PCART 文本提取产物（见待确认点 ②）
    """

class SourceProvider:
    """按 (库,粒度,版本) 整批生成完整 API 定义 + 持久缓存；内部管理 git worktree。

    持久缓存结构：{cache_dir}/{lib}/{type}/{version}/{internal_fqn}.py
    - key 用 internal_fqn（真实源码定义处）：只有它能在源码中唯一定位定义；
      公开名是 __init__ 重导出，多个公开别名共享同一份定义，不重复落盘。
    - 生成单位是「库 × 粒度 × 版本」整批：一次 worktree 切版本，把该版本该粒度
      的全部 API 定义（装饰器 + 签名 + 实现，即完整 FunctionDef/ClassDef 节点
      ast.unparse）一次性提取，每个 API 单独落一个文件；整批完成后写入标记文件
      {version}/.done，作为"该批已生成"的判据，不做预提取（仅按需整批生成）。
    - 语义：get_api 先查该批是否已生成（.done 存在），未生成则整批生成后读取；
      单个文件缺失视为该 API 在该版本无源码定义（不触发重提）。

    __init__(self, lib_name: str, lib_repo_path: str, cache_dir: str,
             worktrees_root: Optional[str] = None)

    def get_api(self, internal_fqn: str, api_type: str, version: str) -> Optional[str]:
        \"\"\"返回 internal_fqn 在 version 的完整 API 定义文本；该批未生成则整批生成。

        输入参数：
            internal_fqn (str)：真实源码定义处 FQN（如 pandas.core.frame.DataFrame.append）。
            api_type (str)：'class' | 'function' | 'method'，决定粒度目录。
            version (str)：版本号。
        返回值：
            Optional[str]：完整定义文本（装饰器+签名+实现 ast.unparse）；
                提取失败 / 源码无此定义返回 None。
        \"\"\"

    def ensure_batch(self, api_type: str, version: str) -> None:
        \"\"\"确保 (lib, api_type, version) 批已生成；已生成则无操作，否则整批提取落盘。\"\"\"

    def source_dir(self, version: str) -> str:
        \"\"\"返回 version 对应源码目录（worktree，进程内复用）。\"\"\"

    def close(self) -> None:
        \"\"\"清理全部 worktree（Pipeline 结束调用）；缓存文件与 .done 保留。\"\"\"
    """
```

### 伪代码（load_knowledge_base）

```
for each version file in {lib_name}/{version}.json 或 {lib_name}{version}（文本格式）:
    records = {}
    for line in file:                       # 忽略分节标记 ----…---- / 空行
        if line.startswith('A:'):           # 赋值行 A:fqn->value
            record = APIRecord(fqn=target, signature='', alias_of=value)
        else:                               # 定义行 fqn(args)->ret
            record = APIRecord(fqn=行头, signature=参数+返回, alias_of=None)
        records[record.fqn] = record
    kb.versions 升序；kb._records[version] = records
```

> 注：当前阶段全量解析（含赋值行与公开名行），不做行类型区分；
> type 由 Configure 的 api_type 提供（待确认点 ③），kb 不推断。

### 伪代码（SourceProvider）

```
ensure_batch(api_type, version):
    if {cache}/{lib}/{api_type}/{version}/.done 存在: return      # 该批已生成
    src = source_dir(version)                         # worktree 进程内复用
    # 全库扫描：ast 遍历该版本全部 .py/.pyi，按粒度收集
    #   class → 全部 ClassDef 节点 / function → 模块级 FunctionDef /
    #   method → 类内 FunctionDef
    # 只看上述真实定义；赋值别名、__init__ 重导出（导入生成的 API）一律不管
    # 每个节点：按其源码定义处路径得 internal_fqn；
    #   完整定义 = ast.unparse(整节点)      # 装饰器 + 签名 + 实现
    #   落盘 {cache}/{lib}/{api_type}/{version}/{internal_fqn}.py
    # 全部落盘后写 {cache}/{lib}/{api_type}/{version}/.done
    # 个别节点提取失败 → 跳过该文件（显式打印），不影响批标记
get_api(internal_fqn, api_type, version):
    ensure_batch(api_type, version)
    return 读 {cache}/{lib}/{api_type}/{version}/{internal_fqn}.py 或 None
```

### 待确认点
- ② **知识库格式**：已确认 **A** —— 直接解析现有 PCART 文本提取产物（零改动）。
- ③ **type 来源**：已确认 —— 不进 kb、不推断；由 Configure 手动提供 `api_type`（class/function/method），全程用于同类型比对；候选 type 由 CodeCache 粒度目录天然分类。

---

## 3. Adjust/getBoundary.py —— 定位失效相邻版本对

### 定位
按存在性判定 fqn 的失效边界，产出相邻版本对 `(Vpre, Vpost)`。**只查 KnowledgeBase，不碰源码**。

### 接口

```python
def get_boundary(fqn: str, start_version: str, target_version: str,
                 kb: KnowledgeBase, versions: List[str]) -> BoundaryResult:
    """定位 fqn 从 start_version 起到 target_version 的失效相邻版本对。

    输入参数：
        fqn (str)：待查 API 公开名（初始 = 用户 old_api_fqn；迭代 = 候选 FQN）。
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
```

### 伪代码

```
i = index(start_version)            # ValueError: start 不在 versions
if not kb.exists(fqn, start_version):
    return NOT_FOUND
prev = start_version
for version in versions[i+1 : index(target_version)+1]:
    if kb.exists(fqn, version):
        prev = version
        continue
    return DEPRECATED(vpre=prev, vpost=version)   # 取【第一个】失效边界
return NOT_DEPRECATED                             # 到 Vt 仍存在
```

### 待确认点
- ① **复现处理**：已确认 -- 只取第一个失效边界（fqn 2.0 消失、3.0 复现 → 取 (1.x, 2.0) 即停）。与 design §9.1 visited 简化一致。
- ①b **起点语义**：已确认 -- 可重入（从 start 查起，不做全区间重复扫描）。

> 注：存在性判定**含别名行**--kb 解析时 A: 行也作为记录（fqn = 赋值目标），
> fqn 以"别名形态"存在的版本 exists 同样为 True；边界自然落在名字
> （定义或别名形态都消失）第一次彻底不存在的版本。Vpre 中 apiX 的形态
> （别名 or 定义）由 resolve_api 区分并更正。

---

## 4. Adjust/resolveApi.py -- 调整待分析 API 到实质 API

### 定位
get_boundary 给出 (Vpre, Vpost) 后，apiX 在 Vpre 可能是**别名形态**（`A: apiX->apiB`，即 apiX = apiB）或**定义形态**。本模块把 apiX 更正为其实质 API：别名更正为赋值目标、定义则识别转发/嵌套调用并更正为被调 API；更正可递归（apiB 自身可能又转发/别名），供相似度推荐作为原 API。

### 接口

```python
def resolve_api(fqn: str, version: str, api_type: str, kb: KnowledgeBase,
                provider: SourceProvider) -> ResolvedApi:
    """把 fqn 更正为实质 API（别名/嵌套调用更正，递归 + visited 防环）。

    输入参数：
        fqn (str)：待分析 API 完整名（= get_boundary 的 Vpre 中仍存在的 API，
            可能以别名或定义形态存在）。
        version (str)：fqn 所在版本（= vpre）。
        api_type (str)：'class' | 'function' | 'method'（来自 Task.api_type）。
        kb (KnowledgeBase)：alias_of 查询与"是否本库 API"验证。
        provider (SourceProvider)：按需提取 API 定义（CodeCache 整批产物）。
    返回值：
        ResolvedApi：
          kind='alias'    -> Vpre 中 fqn 是别名（A: fqn->value），resolved_fqn 为
                             递归更正 value 目标后的实质 API；
          kind='forward'  -> 定义为（可带 print/warnings 前置的）单个 return B(...)，
                             resolved_fqn = 递归更正后的 B；
          kind='nested'   -> 定义内含调用但非纯转发，resolved_fqn = 主调用的更正结果（近似）；
          kind='direct'   -> 无需更正，resolved_fqn = fqn；
          kind='alias_external'  -> 别名目标是外部 API（非本库）：空推荐死分支；
          kind='nested_external' -> 嵌套目标是外部 API（非本库）：不更正，
                             resolved_fqn = fqn，用 fqn 自身定义比对（行为同 direct）；
          kind='unknown'  -> 定义提取失败：保守退回直接比对。
        definition (str)：resolved_fqn 在该版本的完整 API 定义文本（供 Recommend
            精算），None 表示无。original_fqn 恒为最外层输入；kind 记录最外层
            第一次更正的类型（递归内部出现 external/unknown 时向最外层透传）。
    """
```

### 伪代码

```
resolve_api(fqn, version, api_type, kb, provider, visited):
    if fqn in visited: return unknown（防环：apiX->apiB->apiX）
    visited.add(fqn)

    # ---- 分支 1：别名形态更正 ----
    rec = kb.get(fqn, version)
    if rec and rec.alias_of:                        # A: fqn -> value
        name = 解析 value：标识符/点链取本身；调用/调用链取第一个被调 API 名
        target = 分层解析(name)（见下表）           # -> 完整 FQN
        if target 为 None 或 非本库 API（kb 无记录）:
            return ResolvedApi(fqn, fqn, 'alias_external', definition=None)
            # Pipeline 检测 alias_external -> 空推荐死分支
        inner = resolve_api(target, version, api_type, kb, provider, visited)
        if inner.kind in ('alias_external', 'unknown'):
            return inner（original_fqn 换为 fqn）    # 死分支/失败向最外层透传
        return ResolvedApi(fqn, inner.resolved_fqn, 'alias', definition=inner.definition)

    # ---- 分支 2：定义形态更正（转发/嵌套识别，AST 判断）----
    definition = provider.get_api(fqn, api_type, version)
    if definition is None:
        return ResolvedApi(fqn, fqn, 'unknown', definition=None)
    name = AST 识别调用目标：
        函数体除 print(...) / warnings.warn(...) 等无害前置外，
        只有一条 return 且 return 单个调用（或单个名字）-> 目标 = 该调用（forward）
        其余定义内含明显调用 -> 目标 = 主调用（nested，近似）
    if name is None:
        return ResolvedApi(fqn, fqn, 'direct', definition=definition)
    target = 分层解析(name)
    if target 为 None 或 非本库 API:
        return ResolvedApi(fqn, fqn, 'nested_external', definition=definition)
        # 不更正：退回用 fqn 自身定义比对（行为同 direct）
    inner = resolve_api(target, version, api_type, kb, provider, visited)
    if inner.kind in ('alias_external', 'unknown'):
        return inner（original_fqn 换为 fqn）
    return ResolvedApi(fqn, inner.resolved_fqn, 'forward'/'nested', definition=inner.definition)
```

### 分层解析（调用名 -> 完整 FQN，层 1-4 全做）

| 层 | 调用名形态 | 解析规则 |
|---|---|---|
| 1 | 完整 FQN / 点链（`pandas.io.common.get_handle`） | 直接作为完整 FQN，kb/CodeCache 验证 |
| 2 | 简单名（`helper`） | 在 fqn 所在同一模块的 AST 中查同名模块级定义 -> `模块前缀.helper` |
| 3 | import 名（`from ._utils import helper`） | 解析该模块 import 语句（绝对/相对导入），映射为 `X.helper` |
| 4 | `self.xxx` / `cls.xxx` | fqn 为方法时，解析为所在类的同名方法 -> `类前缀.xxx` |

> 属性链（`a().b()`）、动态名等无法静态解析的形态：视为解析失败，按"非本库 API"
> 规则处理（别名分支 -> 空推荐死分支；嵌套分支 -> 不更正退回原定义比对）。

### 待确认点
- ④ **转发识别**：已确认 -- 单 return（可带 print/warnings.warn 无害前置）。
- ⑤ **嵌套取目标 + 分层解析**：已确认 -- 取主调用；分层 1-4 全做；解析失败按外部规则处理。
- ⑪ **比对对象**：已确认（撤回壳定义简化）-- 递归更正到实质 API，用其完整定义比对。
- ⑫ **外部 API 失败规则**：已确认 -- 别名目标外部 -> 空推荐；嵌套目标外部 -> 不更正退回原定义比对。

---

## 5. Recommend/similarity.py —— token 相似度

### 定位
两个完整 API 定义文本的 token-based 相似度，返回 [0,1]。**只算文本，不感知 AST 语义**（design §9.5 待定算法，此处定接口 + 默认实现）。

### 接口

```python
# 算法实现：直接复制 /media/he/Rbench/similarity/similarity/tokenBased.py
#   （自包含，仅用标准库 ast/re/textwrap/collections），落地为 Recommend/tokenBased.py。
#   对外统一走本项目薄封装 token_similarity，内部委托 tokenBased：
#     tokenBased.build_representation(source, name_weight=3.0, param_type_weight=4.0,
#                                     return_type_weight=2.0, default_weight=1.0) -> repr
#       # 将一段 API 源码编译为可复用表示（normalized_source/tokens/counter/weights/meta）
#     tokenBased.similarity_from_representation(repr_a, repr_b) -> float
#       # 复用已编译表示算加权 Jaccard，不再重复 tokenize
#     tokenBased.similarity(source_a, source_b, ...) -> float   # 一次性包装

def token_similarity(def_a: str, def_b: str) -> float:
    """计算两段完整 API 定义文本的 token 相似度，越大越相似，返回 [0,1]。

    输入参数：
        def_a (str)：原 API 定义文本（resolve_api 的 definition，完整定义：装饰器+签名+实现）。
        def_b (str)：候选 API 定义文本。
    返回值：
        float：[0,1] 相似度。
    实现：委托 tokenBased.similarity(def_a, def_b)。
        相似度输入是**整个 API 定义文本**（不只是 body），定义为空/无法产生有效 token 时
        算法自身返回 0.0，**不做任何额外退化处理**（用户确认 ⑧）。
    """
```

### 待确认点
- ⑥ **已确认**：算法直接用 Rbench 的 tokenBased 实现（复制至 Recommend/tokenBased.py），不做加权 Jaccard 以外的自定义实现。

---

## 6. Recommend/recommend.py —— 相邻版本对内同类型 Top-k 推荐

### 定位
以调整后 API 的完整定义为原 API，在 `to_version`（= vpost）全库**同类型** API 中检索 Top-k。**不做初筛**：对候选全集逐一计算相似度分数，按分数降序取 top_k（用户确认 ⑦）。

### 接口

```python
def recommend(resolved: ResolvedApi, from_version: str, to_version: str,
              api_type: str, kb: KnowledgeBase, provider: SourceProvider,
              top_k: int) -> List[Candidate]:
    """在 to_version 全库同类型 API 中推荐 Top-k 候选。

    输入参数：
        resolved (ResolvedApi)：resolve_api 的输出（原 API 及其完整定义）。
        from_version (str)：vpre（用于排除原 API 自身）。
        to_version (str)：vpost（检索发生地）。
        kb (KnowledgeBase)：to_version 的记录（候选 fqn 全集来源）。
        provider (SourceProvider)：候选定义从缓存读取。
        top_k (int)：最终返回候选数。
    返回值：
        List[Candidate]：按 similarity 降序的 Top-k；candidate.fqn 为候选完整名，
            api_type = resolved 同 type，evolution_path/local_scores 留空由 Pipeline 填。
    """
```

### 伪代码

```
provider.ensure_batch(api_type, to_version)              # 整批生成该版本该粒度定义
cands_all = 目录 {cache}/{lib}/{api_type}/{to_version}/ 下全部 fqn（文件名即完整名）
cands_all 剔除 fqn == resolved.resolved_fqn（原 API 自身）

# 不做初筛：候选全集逐一读定义、与完整定义比对（用户确认 ⑦）
scored = []
for c in cands_all:
    def_c = provider.get_api(c.fqn, api_type, to_version)
    if def_c is None: continue                       # 提取失败跳过（显式记录）
    sim = token_similarity(resolved.definition, def_c)
    scored.append((sim, c))
return top_k(scored 按 sim 降序) as List[Candidate]
```

### 待确认点
- ⑦ **已确认**：不做粗筛，对候选全集都计算相似度分数，按分数排序取 top_k（接受 pandas 单版本几万 API 的全量计算成本）。
- ⑧ **已确认**：相似度计算用整个 API 定义作为输入（不只看 body），定义为空不额外退化处理（tokenBased 对空输入自然返回 0.0）。

---

## 7. Pipeline/pipeline.py —— 迭代编排 + 汇总

### 定位
唯一的**有状态编排层**：持有 visited 集合与 SourceProvider 生命周期，BFS 组合 Adjust/Recommend 原语，最终合并去重、路径评分、排序。main 保持薄入口。

### 接口

```python
def run_pipeline(task: Task, kb: KnowledgeBase, cache_dir: str = 'CodeCache') -> Result:
    """BFS 迭代展开演化链并汇总最终推荐。

    输入参数：
        task (Task)：分析任务（Vs/Vt/old_api_fqn/top_k/lib_repo_path）。
        kb (KnowledgeBase)：内存知识库。
    返回值：
        Result：
          - status='OK'：candidates 为去重后按最终评分降序的候选列表；
          - 'NOT_DEPRECATED'：old_api_fqn 在 [Vs,Vt] 全程存在（未弃用），candidates 空；
          - 'NOT_FOUND'：old_api_fqn 在 Vs 即不存在，candidates 空；
          - 'NO_CANDIDATE'：检索始终为空，candidates 空；
          - 'ERROR'：error 字段描述异常（如知识库缺失、worktree 失败）。
    """
```

### 伪代码

```
provider = SourceProvider(task.lib_name, task.lib_repo_path, cache_dir)
try:
    versions = kb.versions 截取 [Vs, Vt]
    queue = deque([(task.old_api_fqn, task.source_version, [], [])])  # (fqn, pos, path, scores)
    visited = set()
    final = []                                  # 有效候选（∈ Vt）

    while queue:
        fqn, pos, path, scores = queue.popleft()
        if fqn in visited: continue             # visited 防循环（朴素按 FQN）
        visited.add(fqn)

        boundary = get_boundary(fqn, pos, task.target_version, kb, versions)
        if boundary.status == NOT_DEPRECATED:
            if 初始分支(即 fqn==old_api_fqn): return NOT_DEPRECATED(空结果)
            continue                            # 迭代分支在 Vt 存在 → 已是有效候选的源头，见下
        if boundary.status == NOT_FOUND: continue
        # 迭代分支走到 NOT_DEPRECATED 说明该 fqn 本就 ∈ Vt，
        # 已在"候选 ∈ Vt 检查"处作为有效候选记录，此处只防死分支。

        resolved = resolve_api(fqn, boundary.vpre, task.api_type, kb, provider)
        if resolved.kind == 'alias_external': continue   # 别名指向外部 API -> 空推荐死分支
        cands = recommend(resolved, boundary.vpre, boundary.vpost, task.api_type,
                          kb, provider, task.top_k)

        if not cands: continue                  # 检索为空 → 死分支
        for c in cands:
            new_path  = path + [c.fqn]
            new_scores = scores + [c.similarity]
            if kb.exists(c.fqn, task.target_version):   # 候选 ∈ Vt → 有效
                final.append(Candidate(c.fqn, c.api_type, 0, new_path, new_scores))
            else:                                        # 继续展开，位置严格前移
                queue.append((c.fqn, boundary.vpost, new_path, new_scores))

    # 汇总
    merged = {}                                  # 按 fqn 合并去重，保留路径评分最高者
    for c in final:
        score = ∏(c.local_scores)
        if c.fqn not in merged or score > merged[c.fqn].score:
            merged[c.fqn] = (score, c)
    ranked = sorted(merged.values(), key=score, reverse=True)[:task.top_k]
    return Result(status='OK', candidates=ranked)
finally:
    provider.close()
```

### 待确认点
- ⑨ **合并去重策略**：同一 fqn 多路径到达时保留**评分最高**的路径（弃其他）。确认。
- ⑩ **位置前移保证**：展开位置 = 上一分支 vpost（严格递增），链长 ≤ 版本数，保证终止（design §9.2 结构保证）。无需额外轮数上限。

---

## 8. main.py —— 入口

### 定位
薄入口：解析子命令（build / recommend）→ 调 Knowledge.build / Pipeline → 格式化输出。不承载分析逻辑。

### 接口

```python
def cmd_build(config_path: str) -> None:
    """读取 Configure JSON（lib/repo/区间/jobs）→ Knowledge.build.build_knowledge。"""

def cmd_recommend(config_path: str) -> None:
    """1) 读配置 → 2) 按 [Vs,Vt] 逐版本检查 kb 缺失，缺则明确报错提示先 build
       3) load_knowledge_base → 4) run_pipeline → 5) 格式化输出候选列表
       （candidate_fqn / type / final_score / evolution_path / local_scores）。
    """

def main(argv: List[str]) -> None:
    """argparse：{build, recommend} + -cfg <path> [--job 覆盖字段]。"""
```

### 待确认点
- 无新决策（复述 design §3 两阶段工作流）。

---

## 9. Knowledge 已实现接口（契约记录，不重新设计）

以已实现源码为准，下游模块**只依赖下列签名**：

| 函数 | 签名 | 说明 |
|---|---|---|
| `getVersion.list_versions` | `(repo_path: str) -> List[Tuple[version, tag]]` | git tag → 规范化、过滤预发布、升序 |
| `getSource.checkout_version` | `(repo_path, tag, dest) -> lib_path` | worktree 切版本，返回包根 |
| `getSource.remove_worktree` | `(repo_path, dest) -> None` | 清理 worktree |
| `getSource.worktree_path` | `(lib_name, version, worktrees_root) -> dest` | worktree 路径约定 |
| `extractApi.extract_lib_api` | `(lib_name, version, lib_path, out_dir, package_root) -> None` | 提取单版本落盘 |
| `build.build_knowledge` | `(lib_name, repo_path, out_dir, source_version, target_version, worktrees_root, jobs) -> Dict` | 构建/补齐知识库 |

> 注：`SourceProvider` 将复用 `getSource.checkout_version / remove_worktree` 与
> `extractApi` 的解析能力，但依赖方向为 `Tool → Knowledge`，不反向。

---

## 10. 全局待确认点汇总

| # | 点 | 位置 | 我的倾向 |
|---|---|---|---|
| ① | 失效边界只取第一个（不复现追踪） | Adjust.getBoundary | 接受（与 visited 简化一致） |
| ①b | get_boundary 可重入（从 start 查起） | Adjust.getBoundary | 接受 |
| ② | 知识库格式：直接解析现有文本 | Tool.load_knowledge_base | **已确认 A** |
| ②b | 定义缓存 key=internal_fqn；`{cache}/{lib}/{type}/{version}/{internal_fqn}.py`；整批 + `.done`；无预提取 | Tool.SourceProvider | **已确认**（目录 `CodeCache/`） |
| ③ | type 来源：Configure 手动提供 `api_type`，kb 不推断 | Tool.model.Task | **已确认** |
| ④ | 转发识别 = 单 return（可带 print/warnings 前置） | Adjust.resolveApi | **已确认** |
| ⑤ | 嵌套取主调用；目标分层解析 1-4 全做 | Adjust.resolveApi | **已确认** |
| ⑥ | 相似度算法：复制 Rbench tokenBased（tokenBased.py） | Recommend.similarity | **已确认** |
| ⑦ | 不做初筛：候选全集全量计算相似度，按分数排序 | Recommend.recommend | **已确认** |
| ⑧ | 相似度输入为整个 API 定义；定义空不退化 | Recommend.recommend | **已确认** |
| ⑨ | 合并去重保留评分最高路径 | Pipeline | 倾向 |
| ⑩ | 位置严格前移即终止（无需轮数上限） | Pipeline | 接受 |
| ⑪ | 递归更正到实质 API，用其完整定义比对 | Adjust.resolveApi | **已确认**（撤回壳定义简化） |
| ⑫ | 别名目标外部->空推荐；嵌套目标外部->不更正退回原定义 | Adjust.resolveApi | **已确认** |

---

## 11. 下一步

1. 逐模块 review 本文档（建议按数据流顺序：model → tool → getBoundary → resolveApi → similarity → recommend → pipeline → main）；
2. 每个待确认点拍板后更新本文档状态为"已确认"；
3. 按确认后的接口逐个实现（实现顺序 = 依赖方向：Tool → Adjust → Recommend → Pipeline → main）；
4. Test/fake_libs 按接口构造多版本假库驱动单测。
