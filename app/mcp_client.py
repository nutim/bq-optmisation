"""Authenticated Streamable HTTP MCP client for approved delivery actions."""

import json
from datetime import timedelta
from typing import Any

import httpx
from google.cloud import secretmanager
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class McpGateway:
    """Calls explicitly named tools; MCP mutation tools are never exposed to the LLM."""

    def __init__(self, timeout_seconds: int = 60) -> None:
        self.timeout_seconds = timeout_seconds

    def _headers(self, secret_resource: str) -> dict[str, str]:
        response = secretmanager.SecretManagerServiceClient().access_secret_version(
            request={"name": secret_resource}
        )
        secret = response.payload.data.decode("utf-8").strip()
        try:
            value = json.loads(secret)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            headers = value.get("headers", {})
            if not isinstance(headers, dict):
                raise ValueError("MCP secret JSON 'headers' must be an object")
            result = {str(key): str(item) for key, item in headers.items()}
            authorization = value.get("authorization")
            if authorization:
                result["Authorization"] = str(authorization)
            return result
        token = secret if secret.lower().startswith("bearer ") else f"Bearer {secret}"
        return {"Authorization": token}

    async def call_tool(
        self,
        server_url: str,
        secret_resource: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_seconds)
        async with (
            httpx.AsyncClient(headers=self._headers(secret_resource), timeout=timeout) as client,
            streamable_http_client(server_url, http_client=client) as streams,
            ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
            ) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            available = {tool.name for tool in tools.tools}
            if tool_name not in available:
                message = f"MCP tool {tool_name!r} is unavailable; server offers {available}"
                raise ValueError(message)
            result = await session.call_tool(tool_name, arguments)
            if result.isError:
                messages = [getattr(block, "text", str(block)) for block in result.content]
                raise RuntimeError(f"MCP tool {tool_name} failed: {'; '.join(messages)}")
            return {
                "tool": tool_name,
                "structured_content": result.structuredContent,
                "content": [block.model_dump(mode="json") for block in result.content],
            }
