# Configure 配置文件目录

该目录存放 PEAR 的任务输入配置文件（JSON 格式）。

每个配置文件包含以下字段（camelCase，与 SourcePCART 配置风格一致）：

- `libName`：待分析的 Python 第三方库名称。
- `sourceVersion`：当前使用的库版本（Vs）。
- `targetVersion`：计划迁移的目标库版本（Vt）。
- `oldApiFqn`：待分析的旧 API 完全限定名（FQN，internal_fqn 完整名）。
- `apiType`：`oldApiFqn` 的类型，取 `class` / `function` / `method` 之一
  （手动提供，全程用于同类型比对，知识库不推断 type）。
- `topK`：每轮局部推荐保留的候选数量。
- `libRepoPath`：本地已 clone 的库仓库路径（自行负责 clone，工具只在其上切版本）。

配置字段可通过 CLI 覆盖；底层 Python API 以 `Task` 数据模型为准。

两阶段用法：

```
python main.py build      -cfg example.json   # ① 构建弃用知识库
python main.py recommend  -cfg example.json   # ② 主流程（缺知识库会报错提示）
```
