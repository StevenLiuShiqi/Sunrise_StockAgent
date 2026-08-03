"""进度推送：把当前请求的回调存起来，agent 内部调用 emit() 即可。

用 contextvars（包一层保持原有 .callback 属性接口的类）而不是 threading.local：
LangGraph 对并行 fan-out 的节点（比如深度模式下的看多/看空研究员）会通过内部线程池
执行，threading.local 在那些 worker 线程里看不到父线程设置的回调，会导致这些节点的
进度事件被静默丢弃；contextvars 在线程池提交任务时会正确传播 context，能覆盖这个
场景（实测验证过），且对外接口不变，调用方不用改。
"""

import contextvars

_callback_var: contextvars.ContextVar = contextvars.ContextVar("progress_callback", default=None)


class _EmitterStorage:
    @property
    def callback(self):
        return _callback_var.get()

    @callback.setter
    def callback(self, fn) -> None:
        _callback_var.set(fn)


emitter_storage = _EmitterStorage()


def emit_event(event: dict) -> None:
    fn = emitter_storage.callback
    if fn:
        fn(event)


def emit(msg: str, ticker: str | None = None) -> None:
    emit_event({"type": "progress", "text": msg, "ticker": ticker})
