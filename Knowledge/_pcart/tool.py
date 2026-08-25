## @package tool
#  vendored from SourcePCART/Tool/tool.py (getAst only)
#  从 SourcePCART 复用的最小工具集：仅提取 getAst（文件/API 串 → AST 根）。
#  供同目录下 getDef.py 的 shortenPath 使用。

import ast
from .py2parse import safe_parse


## Get AST for code
## 将代码转化为Ast树
#
#  @param filePath The code file path or API call string
#  @param strFlag Determine the code from file or API: 0 for file; 1 for API call string
#  @return root The parsed AST root, or None if both ast.parse and the Py2
#          fallback (2to3 conversion) fail
def getAst(filePath,strFlag=0): #若strFlag=1,则表明传进来的是一个api，而不是一个路径
    if strFlag==0:
        with open(filePath,'r',encoding='UTF-8') as f:
            s=f.read()
        return safe_parse(s, filePath)
    return safe_parse(filePath, '<api-string>')
