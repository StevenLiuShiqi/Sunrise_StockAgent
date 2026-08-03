"""LLM 输出解析的小工具：DeepSeek 有时会把要求的 JSON 包在 markdown 代码块里。"""


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` / ``` ... ``` 包裹，方便直接 json.loads()。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
