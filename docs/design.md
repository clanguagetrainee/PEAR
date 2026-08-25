# PEAR 设计文档

> **设计日期**：2026-08-24
> **状态**：初稿（模块划分已定稿；两阶段工作流与 Knowledge 轻量化已锁定）
> 组织风格参考 SourcePCART，仅取「目录首字母大写」的命名习惯，目录名按功能贴切命名。
> 本文件记录当前已讨论锁定的设计决策，随讨论推进持续更新。

## 0. 一句话定位

在 Python 第三方库的**版本演化历史**中，沿旧 API 留下的**演化足迹（evolution trail）**，
追踪其在目标版本中的**后继 API（successor）**。

工具只做 replacement API recommendation，不修改用户代码。

## 1. 输入

```
libName        = pandas
sourceVersion  = x.x.x
targetVersion  = y.y.y
oldApiFqn      = pandas.xxx.old_api
topK           = 3
libRepoPath    = /path/to/pandas_repo     # 本地已 clone 的仓库，自行负责 clone
```

三层输入形式，共用同一内部 `Task` 模型（字段 camelCase，与 SourcePCART 配置风格一致）：

1. `Configure/*.json`：任务定义文件（主入口）；
2. CLI：可选覆盖 Configure 字段；
3. Python API：最底层 `run_task(task)`。

## 2. 输出

核心输出：**目标版本中的替代 API 候选列表**，每个候选含：

```
candidate_api_fqn
candidate_api_type
final_score
evolution_path          # A → B → C
local_similarity_scores # s(A,B), s(B,C)
```

无有效候选时返回明确的空结果 / 失败状态。

## 3. 两阶段工作流

**构建知识库与执行分析解耦**，先后执行：

| 阶段 | 入口 | 做什么 |
|---|---|---|
| ① 构建弃用知识库 | `python main.py build -cfg xxx.json` | 逐版本 `git worktree` 取源码 → 提取 API → 构建结构化知识库 → **持久化到磁盘** |
| ② 主流程（推荐） | `python main.py recommend -cfg xxx.json` | 启动时**先检查知识库是否已构建** → 缺失则明确报错并提示先执行 ① → 加载知识库 → Pipeline 分析 |

**知识库缺失检测**（② 启动时）：按 `[Vs, Vt]` 区间逐版本检查
`LibAPIExtraction/{libName}/{version}.json` 是否存在，缺哪个报哪个，如：

```
知识库缺失: LibAPIExtraction/pandas/1.0.5.json
请先执行 python main.py build -cfg xxx.json 构建该版本。
```

## 4. 源码获取与版本号（Knowledge 底座）

- **源码来源**：本地 `libRepoPath`（git 仓库），逐版本用 **`git worktree add <tmp> <tag>`** 切出该版本源码，
  提取完成即 `git worktree remove`。主仓库状态不被污染，可顺序/并行切多个版本。
- **版本序列**：从 `git tag` 解析，**版本号格式化**——只保留数字段
  （`v2.0.0`/`release-2.0` → `2.0.0`），过滤含 rc/beta/dev 的版本，按数字排序后截取 `[Vs, Vt]`。
  库数量有限，个别 tag 不标准时按库针对性地补映射规则。
- **已知局限**：git 源码是"构建前"的；含 `.pyx→.py`、生成文件、submodule 的库 checkout 后可能缺文件，
  属个别库提取不全，不阻塞。

## 5. 弃用知识库（Knowledge）—— 轻量化

**核心职责**：只做**存在性判定** + **type** + **赋值别名识别**。不存实现体、不存模块信息、不存行号。

- **弃用定义**：弃用 = API 在目标版本**被删除**。A 是赋值别名（`A = B`）或转发（body 调 B）都不影响——
  只要 A 在该版本存在，就不算弃用，不需要看 body。
- **知识库记录格式**：

```json
{
  "fqn": "pandas.DataFrame.append",                       // 公开名（__init__ 重导出缩短后）
  "internal_fqn": "pandas.core.frame.DataFrame.append",   // 原始 FQN（含完整模块路径）
  "type": "method",                                       // class / function / method
  "signature": "(self, other, ignore_index=False)->None", // 对齐 PCART 的签名行
  "alias_of": "pandas.core.frame.DataFrame.append"        // 仅赋值别名（A:old->new）时存在
}
```

- **FQN 双保留**：PCART 对每个 API 输出**原始 FQN 与缩短公开名两条**（非有损二选一）。
  我们结构化为 `fqn` + `internal_fqn` 双字段：存在性判定用公开名 `fqn`，
  赋值别名识别用 `alias_of`，都为后续阶段所需，无 body。

**body 按需提取**：实现体**不进入知识库**，只在相似度推荐阶段按需从源码提取（见 §6.6）。

## 6. 模块划分

组织风格参考 SourcePCART，但**仅取其「目录首字母大写」的命名习惯**，目录名按功能贴切命名：
顶层 **首字母大写功能模块目录**（一个目录 = 一个处理阶段），目录内 **camelCase 命名文件**，
顶层 `main.py` 做入口编排。

| 模块（目录） | 职责 | 状态 |
|---|---|---|
| `Knowledge` 弃用知识库 | 构建阶段：`getVersion`（git tag → 版本序列）、`getSource`（git worktree 取各版本源码）、`extractApi`（AST 提取 + FQN + `__init__` 重导出 + 赋值别名）、`build`（构建入口 + 持久化）、`cache` | 实现 |
| `Adjust` 调整待分析对象 | 1) `getBoundary` 按存在性定位相邻弃用版本对 `(Vpre, Vpost)`（**先**）；2) `resolveApi` 相似度准备阶段按需调整待分析 API（别名查知识库 `alias_of`，转发/嵌套查 body） | 实现 |
| `Recommend` 关联替代分析 | 相邻版本对 `Vi→Vi+1` 内，以**调整后 API 的实现体**为原 API，在 `Vi+1` 全库同类型检索，token-based 相似度（`similarity`），Top-k 推荐（`recommend`） | 实现 |
| `Detect` 项目弃用识别 | 基于弃用知识库扫描用户项目，识别弃用 API 调用 | **占位，暂不实现**（工具以主动输入待分析 API 为主） |
| `Repair` 自动修复 | 基于最终推荐结果自动修改用户项目源码 | **占位，暂不实现** |
| `Pipeline` 迭代编排 + 汇总 | BFS 迭代展开（组合 Adjust / Recommend 原语）、visited 防循环、演化路径记录；最终合并去重、路径评分、排序 | 实现 |
| `Tool` 通用基础 | `model` 数据模型（Task / APIRecord / KnowledgeBase / Candidate / Result）、`tool` 通用工具（配置加载 / AST 辅助 / 按需提取实现体共用函数） | 实现 |
| `main.py` 入口 | 解析输入与子命令（build / recommend）→ 调用 Pipeline → 格式化输出 | 实现 |

**依赖方向**（单向，不回头）：

```
main.py → Pipeline → Adjust, Recommend → Knowledge
                              ↑
                   Tool(model) 被所有模块引用
```

Detect / Repair 为占位模块，后续分别挂在 Pipeline 两侧（喂入 / 消费结果）。

**命名说明**：`Recommend` 正式名为**关联替代分析**（correlative replacement analysis），
当前实现采用 token-based 相似度，命名保留"关联替代"以体现方法论而非实现手段。

## 7. 目录结构

```
PEAR/
├── main.py                     # 入口：build / recommend 子命令
├── requirements.txt
├── README.md
├── docs/
│   └── design.md               # 本文件
├── Configure/                  # 任务配置 JSON（camelCase 字段）
│   ├── README.md
│   └── example.json
├── Knowledge/                  # ① 弃用知识库
│   ├── build.py                #   构建入口 + 持久化
│   ├── getVersion.py           #   git tag → 格式化版本序列
│   ├── getSource.py            #   git worktree 取各版本源码
│   ├── extractApi.py           #   AST 提取 + FQN + __init__ 重导出 + 赋值别名
│   └── cache.py                #   缓存管理
├── Adjust/                     # ③ 调整待分析对象
│   ├── getBoundary.py          #   按存在性定位 (Vpre, Vpost)
│   └── resolveApi.py           #   相似度准备：调整 A → 实际实现体 B
├── Recommend/                  # ④ 关联替代分析
│   ├── similarity.py           #   token-based 相似度
│   └── recommend.py            #   相邻版本对内全库同类型 Top-k 推荐
├── Pipeline/
│   └── pipeline.py             # 迭代编排 + 最终汇总（合并/评分/排序）
├── Detect/                     # ② 项目弃用识别（占位，暂不实现）
│   └── __init__.py
├── Repair/                     # ⑤ 自动修复（占位，暂不实现）
│   └── repair.py
├── Tool/                       # 通用基础
│   ├── tool.py                 #   通用工具（配置加载 / AST 辅助 / 按需提取实现体）
│   └── model.py                #   数据模型 dataclasses
├── LibAPIExtraction/           # 知识库持久化：{libName}/{version}.json
├── CodeCache/                  # 完整 API 定义缓存：{lib}/{type}/{version}/{internal_fqn}.py + .done
│                               #   type ∈ class/function/method；(lib,type,version) 整批按需生成
├── Report/                     # 输出结果（数据目录）
└── Test/
    ├── conftest.py
    ├── fixtures/
    │   └── fake_libs/          # 本地构造的多版本假库，驱动测试
    └── test_*.py
```

## 8. 处理流程

```
输入(libName, Vs, Vt, oldApiFqn, topK, libRepoPath)
[阶段①] Knowledge.build
    getVersion: git tag → 格式化/过滤/排序 → 截取 [Vs, Vt]
    getSource:  逐版本 git worktree add <tmp> <tag> → 源码目录
    extractApi: AST 提取 Class/Function/Method + FQN 双保留 + 赋值别名
    build:      持久化 LibAPIExtraction/{libName}/{version}.json
[阶段②] 主流程
    启动: 检查 [Vs,Vt] 各版本知识库缺失 → 缺失则报错提示先执行 ①
    Pipeline BFS 迭代，逐分支循环：
      (a) Adjust.getBoundary  按存在性定位 (Vpre, Vpost)     ← 只查知识库
      (b) Adjust.resolveApi   相似度准备：调整 A→实际实现体 B（别名查 alias_of，转发/嵌套查 body）
      (c) Recommend.recommend Vpost 全库同类型 Top-k，以 B 的实现体为原 API 比对
      (d) 逐候选验证：∈ Vt → 有效候选；否则 visited 检查后继续展开
    Pipeline 汇总：合并重复 FQN → 路径评分(乘积) → 排序
    main 输出目标版本 replacement recommendation 候选列表
```

## 9. 关键设计决策（已锁定）

### 9.1 迭代引擎：BFS + 自然展开

- **BFS** 展开分支，同层分支并行推进。
- **自然展开**：不做 beam 宽度裁剪。
  - 理由：版本区间有限、Top-k 不大、候选里相当比例直接命中 `Vt`，
    真正需要继续展开的分支是少数，规模自然收敛。
- **visited 防循环**：朴素全局按 FQN 记录，已分析过则跳过。
  - 已知简化：不处理 "API 先退役又复现" 的情况
    （同一 FQN 从不同版本位置展开，语义不同，朴素 visited 会误拦）。
  - 升级方案（暂不做）：visited 记录每个 FQN 已展开的**版本位置集合**，
    新位置未被覆盖时才允许再分析。

### 9.2 终止条件（三态）

每个展开分支只有三种结局：

1. **候选 ∈ Vt** → 有效候选，链结束；
2. **该 FQN 已分析过（visited）** → 跳过，分支结束；
3. **检索为空** → 死分支（防御性保留，实际几乎不发生）。

补充结构保证：沿路径展开位置严格前移，链长 ≤ 版本数，整体必然终止。

### 9.3 收敛性观察

- "无法定位后续失效版本" 是**莫须有**的终止条件：
  边界定位失败 ⟺ API 从当前位置到 Vt 一直存在 ⟺ 它就在 Vt 中。
- "到达目标版本边界" 亦被吸收：边界恰为 `(Vt_prev, Vt)` 时，
  检索发生在 Vt 内，候选天然全部 ∈ Vt。
- 初始 API 若定位不到失效边界，即未弃用，返回明确的空结果。

### 9.4 路径评分：乘积规则

- 各轮归一化相似度连乘：`score(path) = ∏ s(hop)`。
- 隐含行为（已确认接受）：hop 越多，多次小数连乘把分压得越低，
  语义上"中间跳越多、不确定性越大"。

### 9.5 局部推荐约束

- 搜索整个第三方库；
- 只比较相同 API 类型（Class↔Class、Function↔Function、Method↔Method）；
- 相似度固定用一种 token-based 算法（具体算法待定）。

### 9.6 弃用判定与 body 按需提取

- **弃用 = 被删除（存在性）**：别名/转发不影响存在性判定，因此 Knowledge 不存 body。
- **流程顺序**：`getBoundary`（存在性）**先**于 `resolveApi`（调整）；
  需要调整待分析 API 时已进入相似度推荐准备阶段。
- **body 按需提取**（不进知识库，仅相似度推荐阶段）：
  - `Adjust.resolveApi`：判定 A 是否为转发/嵌套调用（`def A(): return B()`）时，从源码提取 A 的 body；
    赋值别名直接查知识库 `alias_of`，无需 body；
  - `Recommend.similarity`：取**调整后 API B 的实现体**作为原 API，与 Vpost 候选比对。
  - 共用提取函数放 `Tool`；依赖方向保持 `Adjust/Recommend → Knowledge` 单向，不反向。

### 9.7 迭代与最终汇总独立为 Pipeline 模块

迭代展开 + 最终推荐汇总（合并/评分/排序）不放入 main，而是独立 `Pipeline` 模块。
理由：

1. main 保持薄入口（解析输入 → 调 Pipeline → 输出）；
2. Adjust / Recommend 是**无状态单步原语**，迭代是**组合原语的有状态编排**，
   两个抽象层次分开，原语模块可独立测试；
3. Detect / Repair 未来分别挂在 Pipeline 两侧（喂入 / 消费结果），
   独立模块使接入不触碰 main。

## 10. 待定项

- token-based similarity 具体算法；
- `__init__.py` 重导出解析细节（FQN 双保留的公开名缩短规则）；
- 别名 / 转发识别的判定规则细节；
- Configure JSON schema 的最终定稿（字段命名、必填项）；
- 知识库中「各版本 API 数量」的统计口径与用途（当前定为知识库构建时的统计信息）。

## 11. 测试策略

- **fake_libs**：`Test/fixtures/fake_libs/` 本地构造多版本假库，预置各类演化形态
  （改名、变转发、退役复现、多跳链等），作为主测试驱动。
- 真库（如 pandas）只作最后端到端验证。

## 12. 暂不实现

自动扫描用户项目（Detect 占位）/ 项目 FQN 恢复 / 完整弃用知识库产品化 /
自动修码（Repair 占位）/ 参数迁移 / import 重写 / 行为等价性验证 / GUI /
IDE 插件 / 完整 CLI / 多相似度算法比较 / LLM 与 embedding / 数据流分析 /
完整 call graph / 复杂动态 Python 特性 / 性能优化。
