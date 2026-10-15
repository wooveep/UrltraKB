"""Read a provider's model names without saving drafts or sending user content."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, SecretStr

from ultrakb.application.connection_failures import connection_failure
from ultrakb.application.model_connections import capture_connection


class ModelCatalogRequest(BaseModel):
    base_url: str
    api_key: SecretStr | None = None
    local: bool = False


@dataclass(frozen=True)
class ModelCatalog:
    models: tuple[str, ...] = ()
    message: str = ""


def read_model_catalog(request: ModelCatalogRequest, kb: Path | None = None) -> ModelCatalog:
    """Capture existing credentials under their lease; release it before HTTP."""
    address = request.base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(address)
    except ValueError:
        return ModelCatalog(message="服务地址格式不正确，请检查后重试。")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ModelCatalog(message="请填写完整的 http/https 服务地址后再获取模型。")
    connection = capture_connection(kb)
    if request.api_key is not None:
        key = request.api_key.get_secret_value()
    else:
        # Drafts cannot silently carry a saved credential to another endpoint.
        saved = connection.credentials.base_url
        default = {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com",
        }.get(connection.model.split("/", 1)[0] if "/" in connection.model else "openai")
        key = connection.credentials.api_key if address == (saved or default) else None
    if not request.local and not key:
        return ModelCatalog(message="请填写此服务的 API Key，然后重新获取模型列表。")
    headers = {"Authorization": f"Bearer {key}"} if key and not request.local else {}
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(
                address + ("/api/tags" if request.local else "/models"), headers=headers
            )
            response.raise_for_status()
            payload = response.json()
        entries = (
            payload.get("models" if request.local else "data")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(entries, list):
            raise ValueError("Missing model list")
        names = set()
        for entry in entries:
            name = entry.get("name" if request.local else "id") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Invalid model name")
            names.add(name.strip())
        models = tuple(sorted(names))
        return ModelCatalog(
            models,
            f"已获取 {len(models)} 个模型；请选择支持工具调用的模型，也可手动填写。"
            if models
            else "服务未返回可选模型，可以手动填写模型名称。",
        )
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error, ValueError):
            return ModelCatalog(message="服务返回的模型列表格式无法识别，可以手动填写模型名称。")
        return ModelCatalog(message=connection_failure(error).message + "也可以手动填写模型名称。")
