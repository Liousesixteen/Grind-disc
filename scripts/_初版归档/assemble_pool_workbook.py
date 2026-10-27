# -*- coding: utf-8 -*-
"""组装最终交付工作簿：4 张新表（格式化）+ 8 张旧版对照表（从旧文件原样搬入）。
用法: python assemble_pool_workbook.py <new_xlsx> <old_xlsx> <out1> [out2]
"""
import sys
from copy import copy
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

new_path, old_path, out1 = sys.argv[1], sys.argv[2], sys.argv[3]
out2 = sys.argv[4] if len(sys.argv) > 4 else None

wb = load_workbook(new_path)          # 4 张新表（已有数据，未格式化）
old = load_workbook(old_path)         # 旧工作簿（含 8 张旧版对照表）

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=11)
thin = Side(style="thin", color="BFBFBF")
BORDER = Border(bottom=thin)


def style_new(ws, freeze_cols=1, wrap_cols=()):
    ws.freeze_panes = ws.cell(row=2, column=freeze_cols + 1)
    for c in ws[1]:
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
    ws.row_dimensions[1].height = 32
    for j, col in enumerate(ws.iter_cols(min_row=2, max_row=min(ws.max_row, 300)), start=1):
        vals = [len(str(c.value)) for c in col if c.value is not None]
        w = (max(vals) if vals else 10) + 3
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(w, 9), 60)
    for name in wrap_cols:
        ws.column_dimensions[name].width = 46
        for c in ws[name][1:]:
            c.alignment = Alignment(wrap_text=True, vertical="center")
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=ws.max_column).column_letter}{ws.max_row}"


for title, fc, wrap in [("窗口A·板块取用", 1, ("J", "K")), ("窗口B·全A股池明细", 2, ()),
                        ("窗口C·老龙记忆", 2, ()), ("口径与来源说明", 1, ("B",))]:
    style_new(wb[title], fc, wrap)
wb["口径与来源说明"].column_dimensions["A"].width = 22

# ---- 搬入 8 张旧版对照表（值 + 样式） ----
def copy_sheet(src, dst_wb, title):
    """旧版对照表：搬值 + 基础格式（跨工作簿直接复制 _style 会破坏样式表索引，故不复制样式）"""
    d = dst_wb.create_sheet(title)
    rows = list(src.iter_rows(values_only=True))
    for r in rows:
        d.append(list(r))
    for c in d[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="808080")
        c.font = Font(bold=True, color="FFFFFF")
    d.freeze_panes = "A2"
    for i in range(1, src.max_column + 1):
        letter = d.cell(row=1, column=i).column_letter
        w = src.column_dimensions[letter].width or 12
        d.column_dimensions[letter].width = min(max(w, 9), 40)
    return d


n = 0
for ws in old.worksheets:
    if "20260904旧版" in ws.title:
        copy_sheet(ws, wb, ws.title)
        n += 1

wb.save(out1)
print("保存:", out1, f"（新表 4 + 旧版对照 {n}）")
if out2:
    wb.save(out2)
    print("保存:", out2)
for ws in wb.worksheets:
    print(f"  {ws.title:<30} {ws.max_row} x {ws.max_column}")
