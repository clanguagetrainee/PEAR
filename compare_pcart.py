## @package compare_pcart
#  PCART 备份 vs 新提取 的版本级 API 差异对比脚本
#  职责：对 PCART 备份（SourcePCART/LibAPIExtraction）与新提取
#  （PEAR/LibAPIExtraction）中共同存在的同名版本文件，做两级格式归一化
#  后统计两边 API 行的对称差数量，并按模块前缀聚类展示差异行来源，
#  用于验证新提取流程与 PCART 原版在相同版本下的 API 一致性。
#  纯文本处理，零第三方依赖。
#
#  归一化口径（为何这样归一化）：
#  1. 基础层：分节标记行 ----{40}路径----{40} 中的绝对路径两边不同
#     （site-packages 路径 vs worktree 路径），仅做分节标识，忽略；
#     内容行 strip 首尾空白、忽略空行；排序 + 去重（set）忽略顺序差异。
#  2. unparse 格式层（--normalize 开启）：消除 Python 3.6 vs 3.11
#     ast.unparse 的确定性输出差异，避免格式噪音污染真差异统计——
#     a) 去除所有空白（含 -> 两侧、逗号后空格、lambda 空格）；
#     b) 生成器/推导式元组解包括号归一化：3.6 输出 for(k,v)，3.11 输出
#        fork,v，统一为 fork,v。
#
#  输出：每库共有/仅旧/仅新版本数、归一化后每版本差异数、差异行按
#  模块前缀聚类的分布（定位整块来源差异，如 pandas._version）、
#  抽样差异行内容。

import os
import re
import sys
from collections import Counter

LIBS = ['django', 'matplotlib', 'pandas']
SEP = '-' * 40
SAMPLE_SHOW = 10  # 每个差异版本最多抽样展示的差异行数
NORMALIZE = True  # 是否启用 unparse 格式归一化（默认开启，--basic 关闭）


def normalize_line(line: str) -> str:
    """对单行做 unparse 格式归一化（去空白 + 元组解包括号）。

    输入参数：
        line (str)：API 内容行。
    返回值：
        str：归一化后的行。
    """
    # A: 赋值行：只保留赋值目标（-> 前）。body 随 unparse 版本/2to3 路径
    # 变化（3.6 的 for((x,y),z) vs 3.11 的 for(x,y),z 等嵌套元组展平差异），
    # 下游知识库不存 body，身份对比应只看 target。
    if line.startswith('A:'):
        body = line[2:]
        target = body.split('->', 1)[0]
        return 'A:' + target.strip()
    # 定义行（prefix.func(args)->ret）：去空白消除 unparse 空格差异
    line = re.sub(r'\s+', '', line)
    # 3.6 unparse 对嵌套元组目标输出 for((x,y),z)/for(key,(d,_))，2to3+3.11
    # 侧输出 forx,y,z/forkey,d,_（或保留内层括号），统一把 for 目标内的
    # 括号去掉、叶子名字按序展平（两侧应用同一变换，不影响一致性判定）。
    line = re.sub(r'for\(((?:\([^()]*\)|[^(),]|,)+)\)',
                  lambda m: 'for' + m.group(1).replace('(', '').replace(')', ''),
                  line)
    return line


# 源码 vs 安装产物 的结构性来源差异标记：PCART 从 site-packages（安装产物）
# 提取，新提取从 git 源码 worktree 提取，下述内容天然只在源码侧出现，
# 非提取 bug，统计真差异时剔除。
SOURCE_EXTRA_MARKERS = ('_version.', '.tests.', '.bin.', 'django.bin')


def is_source_extra(line: str) -> bool:
    """判断差异行是否属于源码侧特有的结构性内容（_version/tests/bin）。

    输入参数：
        line (str)：API 内容行。
    返回值：
        bool：True 表示该行属于来源差异，应从真差异统计中剔除。
    """
    body = line[2:] if line.startswith('A:') else line
    return any(m in body for m in SOURCE_EXTRA_MARKERS)


def parse_api_set(path: str):
    """解析单个版本提取文件，返回归一化后的 API 行集合。

    输入参数：
        path (str)：提取文件路径。
    返回值：
        set[str]：内容行集合（忽略分节标记行与空行，归一化后放入）。
    """
    s = set()
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            if line.startswith(SEP):
                continue
            line = line.strip()
            s.add(normalize_line(line) if NORMALIZE else line)
    return s


def build_global_fqn_set(root: str, lib: str) -> set:
    """构建一个库在某个提取根下的全局 API 身份 FQN 集合（跨全部版本去重）。

    身份 FQN 定义：A: 赋值行取赋值目标（A:prefix.target），定义行取
    归一化后的完整行（FQN+完整参数），与 per-version 对比口径一致。

    输入参数：
        root (str)：提取根目录（旧备份或新提取）。
        lib (str)：库名。
    返回值：
        set[str]：全局去重后的身份 FQN 集合。
    """
    s = set()
    d = os.path.join(root, lib)
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        with open(p, encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line.strip():
                    continue
                if line.startswith(SEP):
                    continue
                s.add(normalize_line(line.strip()))
    return s


def global_compare(old_root: str, new_root: str) -> None:
    """跨全部版本的全局 FQN 集合对比：共同 / PCART 独有 / 新提取独有。

    输入参数：
        old_root (str)：PCART 备份根目录。
        new_root (str)：新提取根目录。
    返回值：
        None，结果直接打印到 stdout。
    """
    print(f"\n{'#'*70}\n[全局 FQN 集合对比]（身份口径，跨全部版本去重）")
    for lib in LIBS:
        old_set = build_global_fqn_set(old_root, lib)
        new_set = build_global_fqn_set(new_root, lib)
        common = old_set & new_set
        old_only = old_set - new_set
        new_only = new_set - old_set
        print(f"\n[{lib}] PCART 全局 FQN {len(old_set)} / 新提取全局 FQN {len(new_set)}")
        print(f"  共同 FQN {len(common)} / PCART 独有 FQN {len(old_only)} / 新提取独有（新增）FQN {len(new_only)}")


def module_prefix(line: str) -> str:
    """提取差异行的模块级前缀（前两段，A: 前缀剥掉），用于聚类。

    输入参数：
        line (str)：API 内容行。
    返回值：
        str：形如 'pandas.core.generic' 或 'pandas._version' 的前缀。
    """
    body = line[2:] if line.startswith('A:') else line
    parts = body.split('.')
    return '.'.join(parts[:2])


def main(old_root: str, new_root: str) -> None:
    """对比两个提取根目录下三库的共同版本 API 差异。

    输入参数：
        old_root (str)：PCART 备份根目录。
        new_root (str)：新提取根目录。
    返回值：
        None，结果直接打印到 stdout。
    """
    grand = {'common': 0, 'diff_versions': 0,
             'old_only_total': 0, 'new_only_total': 0}
    print(f"[归一化] unparse 格式归一化：{'开启' if NORMALIZE else '关闭（--basic）'}")
    for lib in LIBS:
        old_dir = os.path.join(old_root, lib)
        new_dir = os.path.join(new_root, lib)
        old_files = {f for f in os.listdir(old_dir)
                     if os.path.isfile(os.path.join(old_dir, f))}
        new_files = {f for f in os.listdir(new_dir)
                     if os.path.isfile(os.path.join(new_dir, f))}
        common = sorted(old_files & new_files)
        only_old = sorted(old_files - new_files)
        only_new = sorted(new_files - old_files)

        stats = []
        for f in common:
            old_set = parse_api_set(os.path.join(old_dir, f))
            new_set = parse_api_set(os.path.join(new_dir, f))
            # 真差异（剔除来源差异）统计
            real_old_only = {x for x in old_set - new_set if not is_source_extra(x)}
            real_new_only = {x for x in new_set - old_set if not is_source_extra(x)}
            stats.append((f, len(old_set), len(new_set),
                          len(real_old_only), len(real_new_only)))

        zero = [x for x in stats if x[3] == 0 and x[4] == 0]
        nonzero = [x for x in stats if x[3] != 0 or x[4] != 0]
        old_only_total = sum(x[3] for x in stats)
        new_only_total = sum(x[4] for x in stats)

        print(f"\n{'='*70}\n[{lib}] 共有版本 {len(common)} / 仅旧有 {len(only_old)} / 仅新有 {len(only_new)}")
        print(f"  -- 完全一致版本 {len(zero)} / 存在差异版本 {len(nonzero)}")
        print(f"  -- 归一化后【真差异】总量（已剔除 _version/tests/bin 来源差异）：旧独有 {old_only_total} 行 / 新独有 {new_only_total} 行")

        if only_old:
            print(f"  仅旧有版本（{len(only_old)}）: {', '.join(only_old[:10])}{' ...' if len(only_old) > 10 else ''}")
        if only_new:
            print(f"  仅新有版本（{len(only_new)}）: {', '.join(only_new[:10])}{' ...' if len(only_new) > 10 else ''}")

        nonzero_sorted = sorted(nonzero, key=lambda x: x[3] + x[4], reverse=True)
        print("  归一化后差异最大的版本（版本, 旧API, 新API, 旧独有, 新独有）：")
        for f, o, n, oo, nn in nonzero_sorted[:6]:
            print(f"    {f:<20} 旧{o:<6} 新{n:<6} 旧独有{oo:<5} 新独有{nn}")

        # 真差异行按模块前缀聚类（聚合所有差异版本，剔除来源差异）
        old_only_counter = Counter()
        new_only_counter = Counter()
        for f, o, n, oo, nn in nonzero:
            old_set = parse_api_set(os.path.join(old_dir, f))
            new_set = parse_api_set(os.path.join(new_dir, f))
            for line in old_set - new_set:
                if not is_source_extra(line):
                    old_only_counter[module_prefix(line)] += 1
            for line in new_set - old_set:
                if not is_source_extra(line):
                    new_only_counter[module_prefix(line)] += 1
        print("  旧独有真差异按模块前缀分布（Top8）：")
        for pref, cnt in old_only_counter.most_common(8):
            print(f"    {pref:<30} {cnt}")
        print("  新独有真差异按模块前缀分布（Top8）：")
        for pref, cnt in new_only_counter.most_common(8):
            print(f"    {pref:<30} {cnt}")

        # 抽样展示真差异行（最大差异版本）
        if nonzero_sorted:
            f, o, n, oo, nn = nonzero_sorted[0]
            old_set = parse_api_set(os.path.join(old_dir, f))
            new_set = parse_api_set(os.path.join(new_dir, f))
            print(f"\n  --- 抽样真差异行 [{f}]（旧独有 {oo} / 新独有 {nn}）---")
            for line in sorted(x for x in old_set - new_set if not is_source_extra(x))[:SAMPLE_SHOW]:
                print(f"    [旧有] {line}")
            for line in sorted(x for x in new_set - old_set if not is_source_extra(x))[:SAMPLE_SHOW]:
                print(f"    [新有] {line}")

        grand['common'] += len(common)
        grand['diff_versions'] += len(nonzero)
        grand['old_only_total'] += old_only_total
        grand['new_only_total'] += new_only_total

    print(f"\n{'='*70}\n[汇总] 共有版本 {grand['common']} / 存在差异版本 {grand['diff_versions']} "
          f"/ 旧独有差异行 {grand['old_only_total']} / 新独有差异行 {grand['new_only_total']}")
    global_compare(old_root, new_root)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    NORMALIZE = '--basic' not in sys.argv
    default_old = '/home/he/SourcePCART/LibAPIExtraction'
    default_new = '/home/he/PEAR/LibAPIExtraction'
    old = args[0] if len(args) > 0 else default_old
    new = args[1] if len(args) > 1 else default_new
    main(old, new)
