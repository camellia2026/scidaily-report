"""每日科学前沿报告。

把「AI 科技新闻日报」的骨架换到「全球科学前沿文献」这个知识域上。

与原项目的根本差异：原项目建立在 RSS 之上，而顶刊没有可用的 RSS
（JAMA 403、JMLR 不在 Crossref、TPAMI/IJCV/SISC/Acta Numerica 无 feed）。
所以采集层从「抓 feed」重写成「查学术 API」，主力是 OpenAlex。

八层结构与模块的对应关系：

    1. 配置      settings.py
    2. 状态      state.py
    3. 采集      sources/          openalex pubmed crossref rss  + merge
    4. 过滤      filter.py         去重 / 分级 / 分区 / 预打分
    5. 速度      velocity.py       引用速度热文（独立通道）
    6. 生成      ai.py             LLM -> Digest（中英双语）
    7. 校验渲染  render.py         ★ 反幻觉闸门
    8. 推送      notify.py         邮件
    编排         main.py           含 CLI 与退出码纪律
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
