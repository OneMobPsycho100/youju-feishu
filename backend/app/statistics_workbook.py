"""统计报表共用的工作表样式与文本单元格。"""
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


def append_row(sheet, values, *, header=False):
    """按文本保存金额、编号和用户填写内容，避免丢失精度或将内容识别为公式。"""
    cells = []
    for value in values:
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type, cell.number_format = "s", "@"
        if header:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="1665F5")
        cells.append(cell)
    sheet.append(cells)


def create_sheet(workbook, title, headers):
    sheet = workbook.create_sheet(title)
    sheet.freeze_panes = "A2"
    for index in range(1, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 26
    append_row(sheet, headers, header=True)
    return sheet
