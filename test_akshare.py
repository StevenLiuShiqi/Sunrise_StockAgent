"""
AKShare 连接测试脚本
运行：python test_akshare.py
"""
# curl_cffi 提供浏览器级 TLS 指纹，绕过东方财富对 urllib3 的识别拦截
from curl_cffi import requests as _cffi_req

class _BrowserSession:
    def get(self, url, **kwargs):
        kwargs.setdefault("impersonate", "chrome110")
        return _cffi_req.get(url, **kwargs)

import akshare.stock_feature.stock_hist_em as _em
_em.requests = _BrowserSession()

import akshare as ak
from datetime import datetime, timedelta

TICKER = "600570"
end   = datetime.now().strftime("%Y%m%d")
start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

print(f"拉取 {TICKER} 近30日行情（{start} → {end}）...")

try:
    df = ak.stock_zh_a_hist(
        symbol=TICKER, period="daily",
        start_date=start, end_date=end, adjust="qfq",
    )
    print(f"成功！共 {len(df)} 行\n")
    print(df[["日期", "收盘", "成交量"]].tail(5).to_string(index=False))
except Exception as e:
    import traceback
    print(f"失败：{type(e).__name__}")
    traceback.print_exc()
