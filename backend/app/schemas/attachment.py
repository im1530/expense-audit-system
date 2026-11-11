"""
附件模块数据模型（发票/凭证）
    上传接口与列表接口共用 AttachmentResponse：
        - 不返回磁盘路径（file_path），只返回签名下载地址 url —— 存储结构不外泄；
        - file_size 用字节数，前端自行格式化显示（KB/MB）。
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel



# 附件出参
class AttachmentResponse(BaseModel):
    """
    附件出参模型
        :ivar id: 附件ID
        :ivar expense_item_id: 所属报销明细ID
        :ivar file_name: 原始文件名（展示用）
        :ivar content_type: 真实 MIME 类型（按文件头嗅探，不是浏览器声明的值）
        :ivar file_size: 文件字节数
        :ivar uploaded_by: 上传人用户ID
        :ivar uploaded_by_name: 上传人姓名，列表里便于直接展示
        :ivar url: 带短期签名的下载地址（可直接放进 img/iframe，过期需重新获取）
        :ivar created_at: 上传时间
    """
    id: int                                 # 附件ID
    expense_item_id: int                    # 所属报销明细ID
    file_name: str                          # 原始文件名
    content_type: str                       # 真实 MIME
    file_size: int                          # 字节数
    uploaded_by: int                        # 上传人ID
    uploaded_by_name: Optional[str] = None  # 上传人姓名
    url: str                                # 签名下载地址
    created_at: Optional[datetime] = None   # 上传时间
