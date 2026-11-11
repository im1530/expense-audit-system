"""
Excel 导出工具（标准库实现，不依赖 openpyxl / xlsxwriter）
    为什么自己拼 xlsx：项目当前运行环境没装 openpyxl，而导出一张报表只需要"表头 + 若干行 + 简单样式"，
    用 zipfile + OOXML 片段就够，既不用加依赖，也不用担心服务器装包。
    xlsx 本质是一个 zip 包，最小可用结构：
        [Content_Types].xml            声明各部件类型
        _rels/.rels                    指向 workbook
        xl/workbook.xml                工作簿与工作表清单
        xl/_rels/workbook.xml.rels     指向 sheet 与 styles
        xl/styles.xml                  样式（这里只用到"表头加粗"）
        xl/worksheets/sheet1.xml       真正的单元格数据
    单元格写入约定：
        - 字符串用 inlineStr（省掉 sharedStrings 这一层）；
        - 数字/金额写 <v> 数值，保留两位小数由调用方格式化；
        - 日期统一由调用方转成字符串写，避免踩 Excel 的序列日期坑。
"""
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Sequence
from xml.sax.saxutils import escape



# 列号 → Excel 列字母（0→A, 25→Z, 26→AA）
def _column_letter(index: int) -> str:
    """
    把从 0 开始的列下标转成 Excel 列字母
        :param index: 列下标，0 表示第一列
        :return: 列字母，如 0→A、25→Z、26→AA
    """
    # 1.Excel 列号从 1 开始（A=1），先转成 1 基
    letters = ""                                    # 列字母结果
    current = index + 1                             # 1 基列号
    # 2.不停对 26 取余，余数映射成 A-Z 并逆序拼接
    while current > 0:
        current, remainder = divmod(current - 1, 26)   # 商与余数
        letters = chr(65 + remainder) + letters     # 逆序拼接字母
    # 3.返回列字母
    return letters



# 单元格值 → XML
def _cell_xml(ref: str, value, style: int | None = None) -> str:
    """
    生成一个单元格的 XML 片段
        :param ref: 单元格引用，如 B2
        :param value: 值（str / int / float / Decimal / date / datetime / None）
        :param style: 样式序号，1 表示加粗表头；None 表示默认样式
        :return: <c .../> 片段；空值返回空单元格
    """
    style_attr = f' s="{style}"' if style is not None else ""   # 样式属性

    # 1.空值：只占位，保证列对齐
    if value is None or value == "":
        return f'<c r="{ref}"{style_attr}/>'

    # 2.数字：写成数值单元格，Excel 可直接求和
    if isinstance(value, (int, float, Decimal)):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'

    # 3.日期：统一转成字符串，避免 Excel 序列号/时区差异
    if isinstance(value, (datetime, date)):
        text = value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value.strftime("%Y-%m-%d")   # 日期文本
        return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{escape(text)}</t></is></c>'

    # 4.其他一律按字符串处理，并做 XML 转义
    return f'<c r="{ref}"{style_attr} t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'



# 生成列宽配置
def _cols_xml(widths: Sequence[int] | None, column_count: int) -> str:
    """
    生成 <cols> 片段（设置列宽，便于阅读）
        :param widths: 各列宽度（单位：字符数），可为 None
        :param column_count: 总列数
        :return: <cols>...</cols>，无宽度配置时返回空串
    """
    # 1.没有宽度配置就不生成 <cols>，让 Excel 用默认列宽
    if not widths:
        return ""
    # 2.逐列取宽度：配置里没给到的列用 18
    parts = []                                      # 列宽片段
    for index in range(column_count):
        width = widths[index] if index < len(widths) else 18   # 列宽
        letter = _column_letter(index)              # 列字母
        parts.append(f'<col min="{index + 1}" max="{index + 1}" width="{width}" customWidth="1"/>')
    # 3.拼成完整的 <cols> 片段
    return "<cols>" + "".join(parts) + "</cols>"



# 生成 xlsx 文件字节
def build_xlsx(
    sheet_name: str,                                # 工作表名称
    header: Sequence[str],                          # 表头文字
    rows: Iterable[Sequence],                       # 数据行
    widths: Sequence[int] | None = None,            # 列宽配置
) -> bytes:
    """
    生成一个单工作表的 xlsx 文件
        :param sheet_name: 工作表名称（Excel 限制 31 字符，且不能含 : \\ / ? * [ ]）
        :param header: 表头文字
        :param rows: 数据行，每行元素需与表头列数一致
        :param widths: 可选列宽
        :return: xlsx 文件内容（bytes），可直接作为响应体返回
    """
    column_count = len(header)                      # 列数
    safe_sheet_name = escape((sheet_name or "Sheet1")[:31])   # 安全表名

    # 1.表头行
    header_cells = "".join(                         # 表头单元格
        _cell_xml(f"{_column_letter(i)}1", title, style=1)
        for i, title in enumerate(header)
    )
    # 2.数据行
    row_parts = [f'<row r="1">{"".join([header_cells])}</row>']   # 行片段列表
    for row_index, row in enumerate(rows, start=2):
        cells = "".join(                            # 数据单元格
            _cell_xml(f"{_column_letter(col_index)}{row_index}", value)
            for col_index, value in enumerate(row)
        )
        row_parts.append(f'<row r="{row_index}">{cells}</row>')

    last_row = len(row_parts)                       # 末尾行号
    dimension = f"A1:{_column_letter(column_count - 1)}{last_row}"   # 表格范围

    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<dimension ref="{dimension}"/>
<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
<sheetFormatPr defaultRowHeight="15"/>
{_cols_xml(widths, column_count)}
<sheetData>{"".join(row_parts)}</sheetData>
<autoFilter ref="{dimension}"/>
</worksheet>"""                                  # 工作表 XML

    workbook_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{safe_sheet_name}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""                                   # 工作簿 XML

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
  <font><sz val="11"/><name val="Calibri"/></font>
  <font><b/><sz val="11"/><name val="Calibri"/></font>
</fonts>
<fills count="2">
  <fill><patternFill patternType="none"/></fill>
  <fill><patternFill patternType="gray125"/></fill>
</fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
  <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""                                 # 样式 XML

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""                                      # 内容类型 XML

    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""                             # 根关系 XML

    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""                             # 工作簿关系 XML

    # 3.打包成 zip（xlsx 就是 zip）
    buffer = io.BytesIO()                           # 输出缓冲区
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return buffer.getvalue()
