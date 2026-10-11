"""课程 common/ 包的精简 vendored 版本。

从「每日ai发展报告」项目的 common/ 拷贝，只保留本工程真正用到的 7 个模块：

    config.py        .env -> Config（provider/model/base_url/api_key）
    llm.py           多后端 LLM 适配（ollama | deepseek | anthropic | mock）
    mock_llm.py      离线脚本化假模型（冒烟测试用）
    messages.py      OpenAI/Anthropic 消息格式互转
    structured.py    prompt + JSON 抽取 + pydantic 校验 + 修复重试
    console.py       Windows 控制台 UTF-8 修复与日志分色
    reliability.py   transient 判定 + 指数退避重试

刻意去掉的 6 个模块（经实测在本工程中为死代码，约占原包 46%）：
    agent.py / context.py / hitl.py / subagent.py / tools.py / tracing.py
—— 它们服务于「工具调用 / 多智能体 / 上下文压缩」拓扑，本流水线是单次
   call_structured，用不到。将来若真的长出 agent 循环，再按需取回。

llm.py 保留了一处对本工程同样有价值的改动：OpenAI 兼容分支上的响应守卫
（resp.choices 为空时抛出带诊断信息的 LLMError），来自原项目
2026-09-25 的事故复盘。
"""
