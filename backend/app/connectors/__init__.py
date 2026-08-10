"""
@Time       : 2026/08/10 16:35
@Author     : zhanglp8181
@File       : __init__.py
@CallChain  : Connector API/DynamicTaskAgent → connectors package → provider adapters
@Description: 导出受控外部连接领域的公共服务与错误契约。
"""

from app.connectors.service import ConnectionError, ConnectionService

__all__ = ["ConnectionError", "ConnectionService"]
