"""
@Time       : 2026/09/04 12:00
@Author     : zhanglp8181
@File       : browser_relay_connector.py
@CallChain  : PyInstaller/宿主机连接器可执行文件 → app.vertical_abilities.browser_relay_connector
@Description: 为 Windows 安装包提供浏览器所在宿主机的 Relay 连接器入口。
"""

from app.vertical_abilities.browser_relay_connector import main


if __name__ == "__main__":
    raise SystemExit(main())
