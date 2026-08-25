# PEAR 主链路测试覆盖说明

测试入口：`python3 -m pytest Test -q`（当前 **53 passed**）。

夹具与假库：

- [conftest.py](conftest.py) — `FakeProvider` 替身（recommend 纯单元）+ `fake_repo` / `fake_kb`（集成）+ `provider`（真实 SourceProvider，resolve 集成测试）
- [fixtures/fake_libs/make_fakelib.py](fixtures/fake_libs/make_fakelib.py) — 4 版本假库（`fakelib/mod.py` + `fakelib/_impl.py`），预置 forward / alias / nested / direct / not_deprecated 五种演化形态，外加跨模块 import / 点链 / 赋值别名 / 函数内局部 import / method self / 库外形态，以及多跳链 `chain_api→mid_api→new_api2` 与带 `__init__` 的类 `Service`

## 主链路与模块对应

| 环节 | 模块 | 测试文件 |
|---|---|---|
| ① 识别相邻版本对 | [Adjust/getBoundary.py](../Adjust/getBoundary.py) | [test_getBoundary.py](test_getBoundary.py) |
| ② 识别待分析 API 指向的 API | [Adjust/importResolver.py](../Adjust/importResolver.py)（[resolveApi.py](../Adjust/resolveApi.py) 薄入口） | [test_resolveApi.py](test_resolveApi.py)、[test_resolveChain.py](test_resolveChain.py) |
| ③ 找到指向 API 的定义 | [Tool/tool.py](../Tool/tool.py)（`SourceProvider`） | [test_sourceProvider.py](test_sourceProvider.py) |
| ④ 相似度计算推荐 | [Recommend/recommend.py](../Recommend/recommend.py) | [test_recommend.py](test_recommend.py) |
| ⑤ 迭代与汇总 | [Pipeline/pipeline.py](../Pipeline/pipeline.py) | [test_pipeline.py](test_pipeline.py) |
| 基础：配置 / 知识库加载 | [Tool/tool.py](../Tool/tool.py) | [test_tool.py](test_tool.py) |

标记约定：✅ 已覆盖（括号内为测试函数）；❌ 未覆盖（并注明原因）。

---

## 环节 ① 识别相邻版本对（getBoundary）

`get_boundary(fqn, start, target, kb, versions)` 三态 + 存在性判定含别名行。

| 情况 | 覆盖 | 测试用例 |
|---|---|---|
| DEPRECATED：fqn 在中间版本消失，vpre/vpost 为消失相邻对 | ✅ | `test_deprecated_first_boundary` |
| DEPRECATED：fqn 以别名行（`A:`）存在也算存在，边界落在别名也消失处 | ✅ | `test_deprecated_alias_line_counts` |
| NOT_DEPRECATED：start..target 全程存在 | ✅ | `test_not_deprecated` |
| NOT_FOUND：start 版本即不存在 | ✅ | `test_not_found` |
| 可重入：从中间版本 start 起查（迭代分支复用） | ✅ | `test_reentrant_from_start` |
| start_version 不在版本序列 → ValueError | ✅ | `test_start_version_not_in_versions` |
| target_version 不在版本序列 → ValueError | ❌ | 与 start 校验对称，未单独测 |
| target_version 早于 start_version → ValueError | ❌ | 逆向区间未测 |

---

## 环节 ② 识别待分析 API 指向的 API（resolveApi → ImportResolver）

`resolve_api` 已改为**薄入口**，委托 [ImportResolver](../Adjust/importResolver.py) 基于**库源码 AST 精确 import/定义定位**：定位 fqn 的节点（定义/赋值/import），赋值别名追 value、定义则识别转发/嵌套调用追被调 API，沿 import 链递归到真实定义（FunctionDef/ClassDef）或库外（空推荐）。旧实现的「层 1-4 同名近似匹配」（层 2 预设同模块、层 3 全局同名唯一匹配）已整体移除——精确解析失败即库外空推荐，宁漏荐不错荐。

### 七种 kind

| 情况 | 覆盖 | 测试用例 |
|---|---|---|
| direct：定义无调用（普通实现） | ✅ | `test_direct` |
| forward：纯转发（return 单个调用） | ✅ | `test_forward` |
| nested：内含调用非纯转发（首个被调 API） | ✅ | `test_nested` |
| alias：赋值别名（追 value 目标） | ✅ | `test_alias` |
| alias_external：赋值指向库外（`np.array` 未 import） | ✅ | `test_alias_external` |
| nested_external：嵌套调用目标库外（`np.where` 未 import） | ✅ | `test_nested_external` |
| unknown：源码无此定义 | ✅ | `test_unknown` |
| class 粒度：无转发/嵌套语义，直接 direct | ✅ | `test_class_direct` |
| alias → forward 链式更正 | ✅ | `test_alias_to_forward_chain` |
| 别名互相指向 → visited 防环 unknown | ✅ | `test_cycle_guarded` |

### 名字精确解析形态（AST import/定义定位）

| 形态 | 规则 | 覆盖 | 测试用例 |
|---|---|---|---|
| 同模块简单名 | 绑定表（def/assign）命中 | ✅ | `test_forward` / `test_nested` / `test_alias` |
| **跨模块 import 别名** | `from ._impl import x` → 追到 `_impl` 定义 | ✅ | `test_cross_module_import_forward` |
| **点链调用** | `from . import _impl` 后 `_impl.target()` 逐段下钻 | ✅ | `test_dotted_chain_call` |
| **跨模块赋值别名** | `x = cross_new_api()`（import 源在别的模块） | ✅ | `test_cross_module_alias` |
| 完整 FQN 调用 | `fakelib.mod.new_api()` 绝对导入直接定位 | ✅ | `test_forward_dotted_full_fqn` |
| 带参数调用 | `new_api(x, y)` 参数丢弃 | ✅ | `test_alias_with_args` |
| **method self 调用** | `self._impl(x)` → 同类方法（精确） | ✅ | `test_method_self_call` |
| **函数内局部 import** | 函数体 `from ._impl import x` 后 `x(...)` | ✅ | `test_local_import_forward` |
| 库外（根名字未 import） | `np.where(x)` → 失败空推荐 | ✅ | `test_external_call_unresolved` |

### 「解析它是谁」与「找到它的定义」是同一个判据

`ImportResolver.resolve` 用 `SourceProvider.locate_module` 定位模块、`module_ast` 解析 AST；解析出 target 后递归 `resolve(target)`，最终在 FunctionDef/ClassDef 节点上 `ast.unparse` 拿定义；而 `SourceProvider.get_api` 用**同一套 internal_fqn 构造规则**。因此：

> **只要 AST 精确定位到本库定义节点，定义必然拿得到**；解析失败（根名字未 import、点链下钻失败、库外）→ `*_external` / `unknown` → 定义拿不到，链路中断（空推荐）。

核心结论（对比旧「同名近似」）：

- **跨模块 import / 点链**（旧层 2 预设同模块会失败）→ 现在经 import 表精确追到定义；
- **完整 FQN**（`fakelib.mod.x`）→ 绝对导入兜底定位；
- **未 import 的根名字**（`np`、短名 `mod`）→ 精确失败（库外），不再同名近似错配。

---

## 环节 ③ 找到指向 API 的定义（SourceProvider）

`SourceProvider` 按（库, 粒度, 版本）整批生成完整定义 + 持久缓存；另提供模块级 AST（`locate_module` / `module_ast`）供 ImportResolver 精确解析。

| 情况 | 覆盖 | 测试用例 |
|---|---|---|
| 整批生成 function 粒度候选全集 | ✅ | `test_list_api_function_v1` |
| get_api 返回完整定义文本（含 `def`） | ✅ | `test_get_api_definition` |
| 幂等：`.done` 标记存在，二次生成 no-op | ✅ | `test_batch_idempotent` |
| 版本演化差异：删除 / 变赋值别名后不再产出定义 | ✅ | `test_version_evolution` |
| class 粒度收集（含类本身定义） | ✅ | `test_class_granularity` |
| method 粒度收集（`__init__`/`__new__`/`__call__` 排除） | ✅ | `test_method_granularity` |
| 嵌套类递归收集 | ❌ | 假库无嵌套类 |
| `.pyi` 补全（对应 `.py` 不存在时纳入） | ❌ | 假库无 stub |
| overload 跳过（非 pyi 文件跳过 overload 装饰器） | ❌ | 假库无 overload |

---

## 环节 ④ 相似度计算推荐（recommend）

`recommend` 在 `to_version` 全库同类型候选上逐一算 token 相似度，降序取 Top-k。

| 情况 | 覆盖 | 测试用例 |
|---|---|---|
| 排除被替代旧 API 自身（`original_fqn`） | ✅ | `test_excludes_original_fqn` |
| 保留实质 API（`resolved_fqn`），相同定义以 1.0 入选 | ✅ | `test_keeps_resolved_fqn_as_candidate` |
| 按相似度降序排序 | ✅ | `test_sorted_desc_and_topk` |
| top_k 截断 | ✅ | `test_sorted_desc_and_topk` |
| 原 API 定义缺失 → 返回空 | ✅ | `test_empty_when_definition_missing` |
| 排除后候选全集为空 → 返回空 | ✅ | `test_empty_when_no_candidates` |
| 候选定义缺失（`get_api` 返回 None）逐项跳过 | ❌ | 假库候选均有定义，skip 分支未触发 |

---

## 环节 ⑤ 迭代与汇总（pipeline）

`run_pipeline` BFS 展开演化链，最终合并去重、路径评分（∏ 各跳相似度）、排序取 top_k。

| 情况 | 覆盖 | 测试用例 |
|---|---|---|
| OK：**单跳** forward → 最终候选评分 1.0、演化路径正确 | ✅ | `test_forward_ok` |
| NOT_DEPRECATED：旧 API 全程存在 | ✅ | `test_not_deprecated` |
| NOT_FOUND：旧 API 在 Vs 即不存在 | ✅ | `test_not_found` |
| NO_CANDIDATE：检索恒空（monkeypatch recommend 置空） | ✅ | `test_no_candidate` |
| ERROR：Vs 不在知识库版本序列 | ✅ | `test_error_bad_version` |
| **多跳迭代**：候选不在 Vt，BFS 继续展开第二跳直至命中 Vt | ✅ | `test_multihop` |
| 汇总去重保留最高分 | ✅ | `test_multihop`（`new_api2` 单跳低分 / 两跳高分，去重留高分） |
| 路径评分 ∏ 连乘（多跳） | ✅ | `test_multihop`（两跳 score < 1.0） |
| `alias_external` 死分支 `continue`（pipeline 集成层） | ❌ | 假库无外部别名 |
| 最终 top_k 截断（多候选竞争排序） | ✅ | `test_multihop`（2 个候选竞争排序） |

---

## 主要未覆盖缺口汇总（按优先级）

1. **`alias_external` 死分支 `continue`（pipeline 集成层）**：`run_pipeline` 里 `resolve_api` 返回 `alias_external` 时 `continue` 跳过，但现有 pipeline 测试用 monkeypatch 置空 recommend 触发 NO_CANDIDATE，未走「外部别名→空推荐死分支」的真实路径。
2. **嵌套类递归收集**：假库暂无嵌套类，`_collect_class` 的嵌套类递归分支未触发。
3. `ImportResolver` 极端表达式：`super().xxx`、`functools.partial(...)` 等按「解析失败即库外」处理，未单测（与 `np.where` 库外判定同分支）。
4. **候选定义缺失跳过**（recommend 逐项 skip）、`get_boundary` 逆向区间与 target 缺失校验、`.pyi` 补全、overload 跳过（假库无对应形态）。

> 说明：以上未覆盖项均为**真实代码路径未触发**，非已知 bug。补齐时优先扩展 `make_fakelib.py` 的版本形态，而非新增 mock——保持集成测试走真实 worktree + 知识库，与既有风格一致。
