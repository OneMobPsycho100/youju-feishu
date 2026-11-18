"""打印转换子进程。通过系统 Python 加载 UNO，转换时屏蔽外部网络。"""
from collections import Counter
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4


REIMBURSEMENT_TITLE = "费用报销单"
A4_WIDTH, A4_HEIGHT = 21000, 29700  # UNO 页样式、页边距和单元格尺寸的单位为 1/100 mm。


def offline() -> None:
    """保留 UNO 本地管道，拦截外部网络套接字。"""
    import seccomp

    policy = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
    for family in (socket.AF_INET, socket.AF_INET6, socket.AF_PACKET):
        policy.add_rule(seccomp.ERRNO(errno.EPERM), "socket", seccomp.Arg(0, seccomp.EQ, family))
    policy.load()


def _is_reimbursement_sheet(sheet) -> bool:
    """通过工作表名称或 A1 的模板标题识别费用报销单。"""
    return (sheet.getName().strip() == REIMBURSEMENT_TITLE
            or sheet.getCellByPosition(0, 0).getString().strip() == REIMBURSEMENT_TITLE)


def _used_area(sheet):
    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    return cursor


def spreadsheet_selection(document, sheet_count: int, tabs: list[int]):
    """按已选工作表构造 PDF 选区，并为紧凑表格设置等比单页打印。"""
    sheets = document.getSheets()
    if sheets.getCount() != sheet_count:
        return None
    selected = [sheets.getByIndex(index) for index in tabs]
    # 含浮动对象的表格仍使用 Calc 的原始打印范围，避免选区裁掉对象。
    if not selected or any(not sheet.IsVisible or sheet.getDrawPage().getCount() for sheet in selected):
        return None
    styles = document.getStyleFamilies().getByName("PageStyles")
    ranges = document.createInstance("com.sun.star.sheet.SheetCellRanges")
    extents = {}
    for sheet in selected:
        cursor = _used_area(sheet)
        size = cursor.Size
        if _is_reimbursement_sheet(sheet):
            cursor.collapseToMergedArea()
        ranges.addRangeAddress(cursor.getRangeAddress(), False)
        width, height = extents.get(sheet.PageStyle, (0, 0))
        extents[sheet.PageStyle] = max(width, size.Width), max(height, size.Height)
    for name, (width, height) in extents.items():
        style = styles.getByName(name)
        if (style.LeftMargin, style.RightMargin, style.TopMargin, style.BottomMargin) == (1905, 1905, 2540, 2540):
            # 无打印设置的工作簿可收窄默认页边距，为表格保留更多纸面宽度。
            style.LeftMargin = style.RightMargin = 1000
            style.TopMargin = style.BottomMargin = 1200
        paper_width = style.Width - style.LeftMargin - style.RightMargin
        paper_height = style.Height - style.TopMargin - style.BottomMargin
        # 缩到六成以下会明显降低可读性。长表保持纵向分页，超宽表保留原分页。
        if paper_width * 5 >= width * 3:
            style.ScaleToPagesX = 1
            style.ScaleToPagesY = 1 if paper_height * 5 >= height * 3 else 0
    return ranges


def _reimbursement_print_area(sheet):
    """返回包含合并单元格的表单打印范围。显式区域按一个完整外框转印。"""
    areas = sheet.getPrintAreas()
    if not areas:
        cursor = _used_area(sheet)
    else:
        area = sheet.getCellRangeByPosition(
            min(area.StartColumn for area in areas), min(area.StartRow for area in areas),
            max(area.EndColumn for area in areas), max(area.EndRow for area in areas),
        )
        cursor = sheet.createCursorByRange(area)
    cursor.collapseToMergedArea()
    # Calc 会分别对多个显式区域分页，合为一个范围后整张报销单才能共用一页。
    if areas:
        print_area = (cursor.getRangeAddress(),)
        if areas != print_area:
            sheet.setPrintAreas(print_area)
    return cursor


def _fit_reimbursement_rows(sheet, area) -> None:
    """由 Calc 补足文字和公式所在行的行高，保留原行高作为下限。"""
    from com.sun.star.sheet.CellFlags import FORMULA, STRING

    content = area.queryContentCells(STRING | FORMULA)
    row_numbers = {
        index for cells in content.getRangeAddresses()
        for index in range(cells.StartRow, cells.EndRow + 1)
    }
    rows = sheet.getRows()
    for index in sorted(row_numbers):
        row = rows.getByIndex(index)
        if not row.IsVisible or row.OptimalHeight:
            continue
        initial_height = row.Height
        row.OptimalHeight = True
        if row.Height < initial_height:
            row.Height = initial_height


def _separate_page_style(document, styles, sheet, original_style):
    """为共享页样式的报销单复制打印属性，使纸型只作用于当前工作表。"""
    name = f"ArchiveReimbursement_{uuid4().hex}"
    style = document.createInstance("com.sun.star.style.PageStyle")
    styles.insertByName(name, style)
    for property_name in (
        "LeftMargin", "RightMargin", "TopMargin", "BottomMargin",
        "CenterHorizontally", "CenterVertically", "PrintGrid", "PrintHeaders", "PrintZeroValues",
        "PrintFormulas", "PrintCharts", "PrintObjects", "PrintDrawing", "FirstPageNumber",
    ):
        style.setPropertyValue(property_name, original_style.getPropertyValue(property_name))
    for part in ("Header", "Footer"):
        enabled = original_style.getPropertyValue(part + "IsOn")
        style.setPropertyValue(part + "IsOn", enabled)
        if enabled:
            for property_name in (
                part + "IsShared", part + "Height", part + "BodyDistance", part + "IsDynamicHeight",
                "FirstPage" + part + "IsShared", "FirstPage" + part + "Content",
                "LeftPage" + part + "Content", "RightPage" + part + "Content",
            ):
                style.setPropertyValue(property_name, original_style.getPropertyValue(property_name))
    sheet.PageStyle = name
    return style


def _fit_reimbursement_sheets(document, selection) -> None:
    """为当前 PDF 选区内的可见报销单设置 A4 单页。无选区时按 Calc 原打印范围处理。"""
    sheets = document.getSheets()
    all_sheets = [sheets.getByIndex(index) for index in range(sheets.getCount())]
    indices = ({address.Sheet for address in selection.getRangeAddresses()}
               if selection is not None else range(len(all_sheets)))
    forms = [all_sheets[index] for index in indices
             if all_sheets[index].IsVisible and _is_reimbursement_sheet(all_sheets[index])]
    if not forms:
        return

    styles = document.getStyleFamilies().getByName("PageStyles")
    style_counts = Counter(sheet.PageStyle for sheet in all_sheets)
    for sheet in forms:
        style_name = sheet.PageStyle
        style = styles.getByName(style_name)
        if style_counts[style_name] > 1:
            style = _separate_page_style(document, styles, sheet, style)
        area = _reimbursement_print_area(sheet)
        _fit_reimbursement_rows(sheet, area)
        size = area.Size
        width, height = max(1, size.Width), max(1, size.Height)
        horizontal_margin = style.LeftMargin + style.RightMargin
        vertical_margin = style.TopMargin + style.BottomMargin
        if style.HeaderIsOn:
            vertical_margin += style.HeaderHeight
        if style.FooterIsOn:
            vertical_margin += style.FooterHeight
        landscape_scale = min(1, (A4_HEIGHT - horizontal_margin) / width, (A4_WIDTH - vertical_margin) / height)
        portrait_scale = min(1, (A4_WIDTH - horizontal_margin) / width, (A4_HEIGHT - vertical_margin) / height)
        landscape = landscape_scale >= portrait_scale
        # UNO 的方向标记不会自动交换纸张尺寸，两个属性都要按选定方向设置。
        style.IsLandscape = landscape
        style.Width, style.Height = (A4_HEIGHT, A4_WIDTH) if landscape else (A4_WIDTH, A4_HEIGHT)
        style.ScaleToPages = 1
        sheet.removeAllManualPageBreaks()


def office(source: Path, target: Path, print_tabs=None) -> None:
    """静默加载文档，禁用宏与链接更新，并按归档打印规则导出。"""
    import uno
    from com.sun.star.beans import PropertyValue

    def prop(name, value):
        property_value = PropertyValue()
        property_value.Name = name
        property_value.Value = value
        return property_value

    pipe = "archive_" + uuid4().hex
    profile = (target.parent / "office-profile").as_uri()
    xlsx = source.suffix.lower() == ".xlsx"
    process = subprocess.Popen(["soffice", "--headless", "--nologo", "--nodefault", "--norestore",
        "--nofirststartwizard", f"-env:UserInstallation={profile}",
        f"--accept=pipe,name={pipe};urp;StarOffice.ServiceManager"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    desktop, document = None, None
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        from com.sun.star.connection import NoConnectException
        for _ in range(80):
            try:
                context = resolver.resolve(f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext")
                desktop = context.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", context)
                break
            except NoConnectException:
                if process.poll() is not None:
                    break
                time.sleep(0.1)
        if desktop is None:
            raise RuntimeError("Office 转换器启动失败")
        # XLSX 已复制到本次转印的临时目录，允许在内存中调整行高，导出只写入 PDF 路径。
        document = desktop.loadComponentFromURL(source.as_uri(), "_blank", 0, (
            prop("Hidden", True), prop("ReadOnly", not xlsx), prop("Silent", True),
            prop("MacroExecutionMode", uno.getConstantByName("com.sun.star.document.MacroExecMode.NEVER_EXECUTE")),
            prop("UpdateDocMode", uno.getConstantByName("com.sun.star.document.UpdateDocMode.NO_UPDATE")),
        ))
        if document is None:
            raise RuntimeError("文档无法读取")
        export = {
            "com.sun.star.sheet.SpreadsheetDocument": "calc_pdf_Export",
            "com.sun.star.presentation.PresentationDocument": "impress_pdf_Export",
            "com.sun.star.text.TextDocument": "writer_pdf_Export",
        }
        filter_name = next((name for service, name in export.items() if document.supportsService(service)), None)
        if not filter_name:
            raise RuntimeError("文档类型无法导出")
        options = (prop("FilterName", filter_name), prop("Overwrite", True))
        if filter_name == "calc_pdf_Export" and xlsx:
            selection = spreadsheet_selection(document, *print_tabs) if print_tabs else None
            _fit_reimbursement_sheets(document, selection)
            if selection is not None:
                filters = uno.Any("[]com.sun.star.beans.PropertyValue", (prop("Selection", selection),))
                options += (prop("FilterData", filters),)
        document.storeToURL(target.as_uri(), options)
    finally:
        try:
            if document is not None:
                document.dispose()
            if desktop is not None:
                desktop.terminate()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main() -> None:
    offline()
    if sys.argv[1] == "office":
        print_tabs = json.loads(sys.argv[4]) if len(sys.argv) > 4 else None
        office(Path(sys.argv[2]), Path(sys.argv[3]), print_tabs)
    else:
        os.execvp("java", sys.argv[2:])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
