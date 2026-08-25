# PEAR

**PEAR** = **P**ython **E**volution-aware **A**PI **R**eplacement tool。

在 Python 第三方库的版本演化历史中，沿旧 API 留下的演化足迹，推荐其在目标版本中的替代 API（successor）。

## 功能目标

输入给定 Python 第三方库、当前版本、目标版本与一个旧 API 的完全限定名（FQN），
基于该库在源版本到目标版本之间的版本演化历史，输出**目标版本中的替代 API 候选列表及其排序**。

核心能力：

- 基于 API 在实际版本演化边界（`Vpre → Vpost`）上的相似度进行局部替代推荐，
  而非直接比较源版本与目标版本；
- 当候选 API 在目标版本中已不存在时，沿其后续演化继续追踪，形成
  `A → B → C` 的演化路径，直到收敛到目标版本中的有效候选；
- 维护多候选演化分支，合并重复候选，按演化路径各轮相似度乘积排序，
  输出目标版本中的最终候选列表。

工具仅负责替代 API 推荐，不修改用户代码。

## 状态

设计阶段：模块框架已就位（Knowledge / Adjust / Recommend / Detect / Repair + Pipeline），设计与实现细节随讨论持续演进（见 `docs/design.md`）。

## 快速使用（占位，接口未定稿）

输入形式：`config.json` 任务文件 + 可选 CLI 覆盖 + 底层 Python API，共用同一任务模型。
