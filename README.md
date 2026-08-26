# PEAR

**PEAR** = **P**ython **E**volution-aware **A**PI **R**eplacement tool。

在 Python 第三方库的版本演化历史中，沿旧 API 留下的演化足迹，推荐其在目标版本中的替代 API（successor）。

## 功能目标

输入给定 Python 第三方库、当前版本（Vs）、目标版本（Vt）与一个旧 API 的完全限定名（FQN），
基于该库在源版本到目标版本之间的版本演化历史，输出**目标版本中的替代 API 候选列表及其排序**。

核心能力：

- 基于 API 在实际版本演化边界（`Vpre → Vpost`）上的相似度进行局部替代推荐，
  而非直接比较源版本与目标版本；
- 当候选 API 在目标版本中已不存在时，沿其后续演化继续追踪，形成
  `A → B → C` 的演化路径，直到收敛到目标版本中的有效候选；
- 维护多候选演化分支，合并重复候选，按演化路径各轮相似度乘积排序，
  输出目标版本中的最终候选列表。

工具仅负责替代 API 推荐，不修改用户代码。

## 核心流程

1. **Knowledge** — 从库的本地 git 仓库按版本提取 API 定义，构建演化知识库；
2. **Adjust** — 定位旧 API 的失效边界（`Vpre` = 失效前最后存在版本，
   `Vpost` = 首次不存在版本），并用 AST 精确解析把别名 / 转发 / import 链
   更正为实质 API（校验目标粒度一致性，跨粒度保留原定义）；
3. **Recommend** — 在边界版本上对候选全集做定义相似度比对，取 Top-k；
4. **Pipeline** — BFS 沿演化足迹迭代展开候选分支，用 branch-and-bound 剪枝
   提前淘汰不可能进入 Top-k 的分支，合并去重后按路径相似度连乘排序；
   每次运行附带完整 `trace`（每跳的边界版本对、更正结果、候选与剪枝去向），
   供诊断分析；
5. **Baseline** — 直接相似度检索（不追踪演化），作为对照基线。

## 目录结构

```
Adjust/            失效边界定位 + AST 解析更正
Baseline/          直接相似度对照基线
Configure/         任务配置样例
Knowledge/         知识库构建 + 版本/源码读取
Pipeline/          BFS 演化追踪主流程
Recommend/         相似度推荐
Tool/              任务模型 + 源码/缓存 Provider
Test/              单元测试（pytest）
EvalData/          批量实验脚本与结果
LibAPIExtraction/  已提取的库版本 API 知识库
Detect/ Repair/    预留模块（未接入主流程）
```

本地运行目录（不入库）：`Libraries/`（第三方库 git 仓库 checkout）、
`CodeCache/`（完整定义缓存）。

## 使用

### 单任务推荐

1. 构建知识库（按 [Vs, Vt] 区间逐版本提取 API 定义）：

   ```bash
   python main.py build -cfg Configure/example.json [--jobs N]
   ```

2. 执行推荐：

   ```bash
   python main.py recommend -cfg Configure/example.json [--cache-dir CodeCache]
   ```

任务配置（`Configure/example.json`）字段：`libName` / `sourceVersion` /
`targetVersion` / `oldApiFqn` / `apiType`（class | function | method）/ `topK` /
`libRepoPath`。

### 批量实验

`EvalData/run_experiment.py` 对 `EvalData/cases.xlsx` 逐行并行跑 PEAR 与 Baseline，
每个推荐任务输出一个 JSON 文件，另附汇总统计：

```bash
python EvalData/run_experiment.py --top-k 20 [--limit N] [--libs pandas] [--workers W]
```

产出（`EvalData/` 下）：

- `PEAR/`、`Baseline/` — 各 161 个推荐任务的原始输出
  （PEAR 含 task + 候选 + trace；Baseline 含 task + 候选）；
- `experiment_summary_top{top_k}.json` — 每实验一条命中记录 + 各库汇总
  （命中数，数值非百分比）；
- `cases.xlsx` — 输入用例。

## 实验评估

在 pandas（目标 3.0.5）、matplotlib（目标 3.11.1）、django（目标 6.0.7）
三个库共 161 个真实替代 API 用例上，PEAR 的各 Top-k 命中率全面领先 Baseline
（明细见 `EvalData/experiment_summary_top20.json`）。

## 状态

核心流程已实现并可运行：具备 AST 精确解析更正、BFS 演化追踪（剪枝 + trace）、
直接相似度对照基线，以及三库 161 用例的批量实验结果与 pytest 测试套件。
