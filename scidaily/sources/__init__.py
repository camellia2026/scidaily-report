"""采集层。

与「抓 RSS」的原项目不同，这里的每个模块是一个**学术 API 适配器**：

    openalex.py   主力。一次 OR 请求覆盖全部期刊 ISSN，自带 locations 聚类
                  （预印本与正式版同一个 work）与 counts_by_year（引用速度原料）
    pubmed.py     医学刊摘要的最佳来源；也是 JAMA 的兜底（它的 RSS 实测 403）
    crossref.py   期刊 ISSN 发现，补漏。注意：摘要基本为 NONE
    rss.py        期刊自带的 TOC feed 与科学要闻
    merge.py      预印本 <-> 正式版合并，以及「版本升级」判定

所有适配器产出同一种记录（Paper / NewsItem），下游与来源无关。
这一层是包对外的门面，所以在这里统一 re-export，调用方不必记住
某个类型究竟定义在 base 还是 merge 里。
"""

from .base import (
    FetchError,
    NewsItem,
    Paper,
    SourceReport,
    decode_abstract_inverted_index,
    http_get_json,
    throttle_wait,
)

__all__ = [
    "FetchError",
    "NewsItem",
    "Paper",
    "SourceReport",
    "decode_abstract_inverted_index",
    "http_get_json",
    "throttle_wait",
]
