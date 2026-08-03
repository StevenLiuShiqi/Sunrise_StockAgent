"""持仓数据加载与持久化。

不在模块级缓存 HOLDINGS/RISK_PROFILE 常量——交易终端下单会直接改写 holdings.json，
如果只在进程启动时读一次，后续交易不会反映到下一次 AI 分析里。改成每次都从磁盘
读，对这么小的 JSON 文件性能完全不是问题，正确性优先。
"""

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


def save_holdings(holdings: list[dict], risk_profile: str) -> None:
    """把持仓列表和整体风险偏好写回 holdings.json。"""
    data = {"risk_profile": risk_profile, "holdings": holdings}
    HOLDINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
