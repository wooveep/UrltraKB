"""Small connection templates checked against official docs on 2026-10-14."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionTemplate:
    name: str
    model: str | None
    base_url: str | None
    guide: str | None
    explanation: str


CONNECTION_TEMPLATES = (
    ConnectionTemplate(
        "自定义 / 已有连接",
        None,
        None,
        None,
        "保留现有连接，或按服务说明手填模型和地址。是否可用由实际测试确认。",
    ),
    ConnectionTemplate(
        "OpenAI",
        "gpt-5.4",
        "https://api.openai.com/v1",
        "https://developers.openai.com/api/docs/quickstart",
        "需要此服务的账号、API Key 和模型权限；示例模型可修改。",
    ),
    ConnectionTemplate(
        "DeepSeek",
        "deepseek/deepseek-flash",
        "https://api.deepseek.com",
        "https://api-docs.deepseek.com/",
        "先在官方平台获取 API Key。Flash 示例支持工具调用，模型名称可手填。",
    ),
    ConnectionTemplate(
        "Ollama 本地服务",
        "ollama_chat/qwen3",
        "http://localhost:11434",
        "https://docs.ollama.com/capabilities/tool-calling",
        "先启动本地服务并准备支持工具调用的模型。本地服务通常无需 API Key。",
    ),
)
