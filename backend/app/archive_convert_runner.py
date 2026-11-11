"""打印转换子进程。通过系统 Python 加载 UNO，转换时屏蔽外部网络。"""
import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4


def offline() -> None:
    """保留 UNO 本地管道，拦截外部网络套接字。"""
    import seccomp

    policy = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
    for family in (socket.AF_INET, socket.AF_INET6, socket.AF_PACKET):
        policy.add_rule(seccomp.ERRNO(errno.EPERM), "socket", seccomp.Arg(0, seccomp.EQ, family))
    policy.load()


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
        cursor = sheet.createCursor()
        cursor.gotoStartOfUsedArea(False)
        cursor.gotoEndOfUsedArea(True)
        ranges.addRangeAddress(cursor.getRangeAddress(), False)
        size = cursor.Size
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


def office(source: Path, target: Path, print_tabs=None) -> None:
    """静默加载文档并导出，禁用宏与链接更新，保留原始打印设置。"""
    import uno
    from com.sun.star.beans import PropertyValue

    def prop(name, value):
        property_value = PropertyValue()
        property_value.Name = name
        property_value.Value = value
        return property_value

    pipe = "archive_" + uuid4().hex
    profile = (target.parent / "office-profile").as_uri()
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
        document = desktop.loadComponentFromURL(source.as_uri(), "_blank", 0, (
            prop("Hidden", True), prop("ReadOnly", True), prop("Silent", True),
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
        if filter_name == "calc_pdf_Export" and print_tabs:
            selection = spreadsheet_selection(document, *print_tabs)
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
