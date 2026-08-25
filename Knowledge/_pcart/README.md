# _pcart（vendored from SourcePCART）

从 `/home/he/SourcePCART` 复用的库 API 提取层，**逻辑保持原样**，仅做最小适配。

## 来源对应

| 本目录文件 | SourcePCART 源文件 |
|---|---|
| `getPath.py` | `Path/getPath.py`（Path 类，DF 模式遍历 .py/.pyi） |
| `getDef.py` | `Extract/getDef.py`（getDefFunction 主提取 + task/getClass/getAssign/shortenPath） |
| `extractDef.py` | `Extract/extractDef.py`（Def2format / FromImport / AssignVisitor） |
| `extractCall.py` | `Extract/extractCall.py`（Import 类，getAssign 依赖） |
| `tool.py` | `Tool/tool.py` 中 `getAst`（仅此函数） |

## vendored 修改点（均在代码内以 `vendored 修改点` 注释标注）

1. **扁平化 import**：PCART 为 namespace package（`from Path.getPath import *`），
   本目录扁平化为同级相对 import。
2. **Def2format.toFormat(file, libPath)**：PCART 按 `site-packages/` 路径切分构造 prefix
   （面向 conda 安装包）；我们的源码来自 git worktree（无 site-packages），
   改为**相对库源码根**构造 prefix。
3. **getDefFunction(args, out_dir='LibAPIExtraction')**：输出目录参数化
   （原版写死相对 CWD 的 `LibAPIExtraction`）；`mkdir` 的静默 `except: pass` 改为
   显式 `os.makedirs(exist_ok=True)`。

## 输出

文本格式（与现有 LibAPIExtraction 文件一致）：`{out_dir}/{libName}/{libName}{version}`
- 赋值别名行：`A:{prefix}.{target}->{value}`
- 定义行：`{prefix}.{name}({args}){ret}`（类/函数/方法同格式）
- 每文件分节：`-{40}{绝对路径}{40}-`，文件内按 API 排序
- `.pyi` 与 `.py` 差集合并（.pyi 独有 stub 保留）
