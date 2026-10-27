#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
章节引用校验器（熔炼规则第四章「编号与引用规范」的落地工具）

做什么：
  校验全工作区对《熔炼系统》/《熔炼规则》的章节引用是否仍有效。
  校验基准 = `熔炼规则.md` 第四章「编号总表」（表格行：| 文件 | 编号 | 节名 |）。

判定：
  ERROR  —— 引用了编号总表里不存在的编号（多半是改号后遗留的旧号），须修。
  WARN   —— 编号有效但附近没写节名（违反「引用三件套」），建议补。
  INFO   —— 工作区仍出现的「熔炼体系」旧名（历史条目除外，仅为提示）。

用法：
  python3 scripts/check_refs.py          # 只报错与统计
  python3 scripts/check_refs.py -v       # 列出全部引用（含 OK）
  python3 scripts/check_refs.py --fix    # 自动把「缺节名」的引用补齐为三件套（只补节名，不改编号）

注意：--fix 只处理 WARN（编号有效但缺节名）。ERROR（旧号遗留）必须人工判断新号后修改，
脚本不做猜测式替换——猜错会把引用指向错误的节，比断链更危险。

退出码：0 = 无 ERROR；1 = 存在 ERROR。

约定：改任何章节编号后必须跑本脚本，0 ERROR 才算完成重编号批次。
"""

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(ROOT, '熔炼规则.md')

# 扫描对象
FILES = ['AGENTS.md', '作业节奏.md', 'DAILY_OPERATING_PLAYBOOK.md',
         'DATA_SOURCE_POLICY.md', 'watchlists/active.md',
         '熔炼系统.md', '熔炼规则.md']
DIRS = ['docs', 'knowledge', 'scripts', '.workbuddy/memory']
EXTS = ('.md', '.py', '.json')
# 跳过：归档、备份、历史日志与报告（历史条目不改）
SKIP_DIRS = {'_归档', 'backups', '_初版归档', 'cache', 'journal', 'reports', '.git'}
DAILY_LOG = re.compile(r'^\d{4}-\d{2}-\d{2}\.md$')

# 匹配形状：文件名后紧跟章节编号，编号可为「阿拉伯小节号」「第N章」「中文数字」三种写法
REF = re.compile(
    r'(熔炼系统|熔炼规则)(?:\.md)?[》》`*\s]*'
    r'((?:第)?[〇一二三四五六七八九十]{1,2}章|\d+\.\d+|[〇一二三四五六七八九十]{1,2})'
    r'(?=[`*\s，。；、）)：:／\-]|$)'
)
# 编号总表行
ROW = re.compile(r'^\|\s*(熔炼系统|熔炼规则)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|')


def load_inventory():
    """从熔炼规则.md 的编号总表解析 {文件: {编号: 节名}}"""
    inv = {'熔炼系统': {}, '熔炼规则': {}}
    text = io.open(RULES, encoding='utf-8').read()
    for line in text.split('\n'):
        m = ROW.match(line.strip())
        if not m:
            continue
        doc, num, name = m.group(1), m.group(2).strip(), m.group(3).strip()
        if num in ('编号', '文件'):
            continue
        inv[doc][num] = name
    return inv


def collect_targets():
    paths = []
    for f in FILES:
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            paths.append(p)
    for d in DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for cur, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for fn in files:
                if not fn.endswith(EXTS):
                    continue
                if DAILY_LOG.match(fn):
                    continue
                paths.append(os.path.join(cur, fn))
    return sorted(set(paths))


def _lcp_len(a, b):
    """两字符串的最长公共前缀长度（按字符比较）。"""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def fix_file(path, inv):
    """把该文件里「编号有效但缺节名」的引用补齐为三件套。只插入节名，不改编号。

    返回 (改动行数, 新文本, 跳过列表)；无改动返回 (0, None, [])。

    已知坑（2026-10-19 修）：原实现只判断「规范节名是否原样出现」，遇到
    **近似写法**（如写了「统一引擎：六层漏斗」而规范名是「统一引擎（层0—层5，
    层号引用天然稳定）」）会误判为「没写节名」并**追加**，造成
    「规范名 + 旧节名」重复（例：熔炼系统 四 统一引擎（层0—层5，层号引用天然稳定） 统一引擎：六层漏斗）。
    故此处增加近似判定：与规范名公共前缀 >= 4 字即视为「已写节名（写法不一致）」，
    **跳过不追加**，并回报给调用方提示人工对齐——自动替换风险太大，容易吃掉正常行文。
    """
    text = io.open(path, encoding='utf-8').read()
    lines = text.split('\n')
    changed = 0
    skipped = []
    for idx, line in enumerate(lines):
        # 逆序替换，避免插入文本后偏移失效
        inserts = []
        for m in REF.finditer(line):
            doc, num = m.group(1), m.group(2).replace('第', '').replace('章', '')
            name = inv[doc].get(num)
            if name is None:
                continue
            tail = line[m.end():m.end() + 40]
            if name in tail:
                continue
            stripped = tail.lstrip()
            if _lcp_len(name, stripped) >= 4:
                # 表格单元格里节名后面常跟下一列，截到「|」为止，提示才干净
                written = stripped.split('|')[0].strip()[:24]
                skipped.append((idx + 1, doc, m.group(2), name, written))
                continue
            inserts.append((m.end(), ' ' + name))
        if not inserts:
            continue
        for pos, s in reversed(inserts):
            line = line[:pos] + s + line[pos:]
        lines[idx] = line
        changed += 1
    if not changed:
        return 0, None, skipped
    return changed, '\n'.join(lines), skipped


def main():
    verbose = '-v' in sys.argv
    do_fix = '--fix' in sys.argv
    inv = load_inventory()
    total = {'熔炼系统': len(inv['熔炼系统']), '熔炼规则': len(inv['熔炼规则'])}
    errors, warns, oks, legacy = [], [], 0, 0

    for path in collect_targets():
        try:
            text = io.open(path, encoding='utf-8').read()
        except (OSError, UnicodeDecodeError):
            continue
        rel = os.path.relpath(path, ROOT)
        legacy += text.count('熔炼体系')
        for i, line in enumerate(text.split('\n'), 1):
            for m in REF.finditer(line):
                doc, num = m.group(1), m.group(2).replace('第', '').replace('章', '')
                name = inv[doc].get(num)
                window = line[m.end():m.end() + 40]
                if name is None:
                    errors.append('%s:%d  ERR  引用「%s %s」不在编号总表（旧号遗留？）' % (rel, i, doc, m.group(2)))
                elif name not in window:
                    warns.append('%s:%d  WARN 编号有效但缺节名：「%s %s」应写作「%s %s %s」'
                                 % (rel, i, doc, m.group(2), doc, m.group(2), name))
                else:
                    oks += 1
                    if verbose:
                        print('%s:%d  OK   %s %s %s' % (rel, i, doc, m.group(2), name))

    if do_fix:
        if errors:
            print('拒绝 --fix：存在 %d 条 ERROR（旧号遗留）。请先人工定新号，再补节名。' % len(errors))
            return 1
        targets = collect_targets()
        fixed_files, fixed_lines, all_skipped = 0, 0, []
        for path in targets:
            try:
                n, new, skipped = fix_file(path, inv)
            except (OSError, UnicodeDecodeError):
                continue
            for ln, doc, num, name, written in skipped:
                all_skipped.append('%s:%d  已写「%s」≠ 规范名「%s」——未自动改，请人工统一'
                                   % (os.path.relpath(path, ROOT), ln, written, name))
            if not n:
                continue
            io.open(path, 'w', encoding='utf-8').write(new)
            fixed_files += 1
            fixed_lines += n
            print('  已补节名：%s（%d 行）' % (os.path.relpath(path, ROOT), n))
        print('补节名完成：%d 个文件 / %d 行。请重跑本脚本确认 WARN 归零。' % (fixed_files, fixed_lines))
        if all_skipped:
            print('\n以下 %d 处已写了节名但与规范名不一致，脚本**不自动替换**（避免吃掉正常行文），需人工统一：' % len(all_skipped))
            for s in all_skipped:
                print('  ' + s)
        return 0

    print('编号总表：熔炼系统 %d 节 / 熔炼规则 %d 节' % (total['熔炼系统'], total['熔炼规则']))
    print('引用统计：OK %d | WARN %d | ERROR %d | 旧名「熔炼体系」出现 %d 次（历史条目除外）'
          % (oks, len(warns), len(errors), legacy))
    for w in warns:
        print('  ' + w)
    for e in errors:
        print('  ' + e)
    if errors:
        print('\n结论：FAILED —— 存在失效引用，请修正后重跑（重编号批次未通过）。')
        return 1
    print('\n结论：PASS —— 0 失效引用' + ('，但有 %d 条建议补节名。' % len(warns) if warns else '，全部符合引用三件套。'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
