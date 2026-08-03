"""配置：代理清理、环境变量加载、LLM/Tavily 客户端、调研域名白名单。

必须在任何 import baostock / akshare 之前执行完毕（清代理环境变量 + 装空 ProxyHandler），
所以 market_data.py 和 cloud_research.py 都会先 `from . import config` 再导入这些库。
"""

import os
import urllib.request
from pathlib import Path

# 清除所有代理环境变量，NO_PROXY=* 告诉 requests 对任何地址都不走代理，
# 同时也屏蔽 macOS 系统代理的自动探测。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# urllib.request 默认会读 macOS 系统代理（getproxies()），
# 装一个空的 ProxyHandler 覆盖掉，让 fetch_valuation 直连腾讯财经。
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

from openai import OpenAI
from tavily import TavilyClient


def _load_env(path: str = ".env") -> None:
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


_load_env()

LLM_API_KEY    = os.environ.get("LLM_API_KEY", "").strip()
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()

_missing_keys = [name for name, value in (
    ("LLM_API_KEY", LLM_API_KEY),
    ("TAVILY_API_KEY", TAVILY_API_KEY),
) if not value]
if _missing_keys:
    raise RuntimeError(
        f"缺少必需的环境变量：{', '.join(_missing_keys)}。"
        "请在 .env 或 Railway 的环境变量配置中设置后再启动。"
    )

llm = OpenAI(
    api_key=LLM_API_KEY,
    base_url="https://api.deepseek.com",
)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

# MODEL_PRO 是推理模型（返回里带 reasoning_content，会先"思考"再输出正文），
# 同样的 max_tokens 下可用的正文长度比 MODEL_STANDARD 少很多——用它的调用点
# 都要把 max_tokens 设得远大于看起来需要的正文长度，留够推理消耗的空间，
# 否则会出现 finish_reason=length 但 content 是空字符串的情况。
MODEL_STANDARD = "deepseek-chat"
MODEL_PRO      = "deepseek-v4-pro"

# 按信噪比分层的域名白名单
DOMAINS_FACTUAL = [
    "cninfo.com.cn", "sse.com.cn", "szse.cn",
    "csrc.gov.cn", "cls.cn",
]
DOMAINS_NEWS = [
    "cls.cn", "yicai.com", "caixin.com",
    "eastmoney.com", "gelonghui.com",
]
DOMAINS_OPINION = [
    "xueqiu.com", "gelonghui.com", "yicai.com",
]
DOMAIN_MAP = {
    "factual": DOMAINS_FACTUAL,
    "news":    DOMAINS_NEWS,
    "opinion": DOMAINS_OPINION,
}
