"""MCP 伺服器的啟動點。

用絕對路徑呼叫這個檔案就能啟動，不必先安裝套件、也不必管工作目錄在哪——
MCP 客戶端啟動子行程時的工作目錄不一定是這裡，`python -m csdmcp` 會找不到模組。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from csdmcp.server import main   # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
