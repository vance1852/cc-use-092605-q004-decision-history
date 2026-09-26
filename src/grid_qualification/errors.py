"""并网审批流程使用的领域错误类型。"""

from __future__ import annotations


class Conflict(Exception):
    """编号相同但内容不同的请求，或并发的相同编号提交，被明确拒绝。"""
