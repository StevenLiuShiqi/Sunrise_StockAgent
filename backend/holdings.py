"""持仓数据加载。"""

import json
from pathlib import Path

HOLDINGS_FILE = Path(__file__).parent.parent / "holdings.json"


def load_holdings() -> tuple[list[dict], str]:
    """从 holdings.json 读取持仓列表和整体风险偏好。"""
    try:
        data = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
        return data["holdings"], data.get("risk_profile", "moderate")
    except FileNotFoundError:
        raise RuntimeError(f"找不到持仓文件：{HOLDINGS_FILE}")
    except (KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"持仓文件格式错误：{e}")


HOLDINGS, RISK_PROFILE = load_holdings()
