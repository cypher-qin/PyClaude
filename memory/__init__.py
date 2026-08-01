"""记忆子系统。

agent.py 不直接 import 本包，只认 memory_integration.py 暴露的函数——
和 mcp_manual/ + mcp_integration.py 是同一个模式：子系统换实现，主程序不动。

    memdir     存储层：记忆长什么样、放在哪
    relevance  选择层：这一轮该注入哪几条
    extract    写入层：从对话里提炼新记忆
    dream      治理层：合并去重、淘汰过时
    session    会话层：单会话内的压缩续接

本包不 import agent.py。要调模型的地方（relevance / extract / dream）都由
调用方把 side_query / fork_agent 传进来——这样包可以脱离网络单测。
"""
