"""Amazon Bedrock (Converse API) via boto3. Optional: ``pip install 'super-agent-x[bedrock]'``."""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

from ...errors import ProviderError
from ...types import ProviderStatus, ProviderTier
from ..base import LLMProvider, LLMRequest, LLMResponse, Readiness, Usage

_AUTH_CODES = {"AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException", "InvalidSignatureException"}
_THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}


class BedrockLLM(LLMProvider):
    name = "bedrock"
    tier = ProviderTier.CLOUD_SECONDARY
    description = "models on Amazon Bedrock (Converse API)"

    def __init__(self, access_key: str | None, secret_key: str | None, region: str | None, model: str | None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.model = model
        self._client: Any = None

    def readiness(self) -> Readiness:
        if not (self.access_key and self.secret_key and self.region):
            return Readiness.not_configured("set AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_REGION")
        if not self.model:
            return Readiness.not_configured("no model selected (set BEDROCK_MODEL_ID)")
        if importlib.util.find_spec("boto3") is None:
            return Readiness.unavailable("the 'boto3' package is not installed (pip install 'super-agent-x[bedrock]')")
        return Readiness.ready()

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "bedrock-runtime", region_name=self.region,
                aws_access_key_id=self.access_key, aws_secret_access_key=self.secret_key,
            )  # fmt: skip
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        from botocore.exceptions import BotoCoreError, ClientError

        system = [request.system] if request.system else []
        system += [m.content for m in request.messages if m.role == "system"]
        kwargs: dict[str, Any] = {
            "modelId": self.model,
            "messages": [
                {"role": m.role, "content": [{"text": m.content}]} for m in request.messages if m.role != "system"
            ],
            "inferenceConfig": {"maxTokens": request.max_tokens},
        }
        if system:
            kwargs["system"] = [{"text": "\n\n".join(system)}]
        try:
            response = await asyncio.to_thread(self._get_client().converse, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            message = exc.response.get("Error", {}).get("Message", str(exc))[:400]
            if code in _AUTH_CODES:
                raise ProviderError(self.name, ProviderStatus.AUTH_ERROR, message) from exc
            if code in _THROTTLE_CODES:
                raise ProviderError(self.name, ProviderStatus.RATE_LIMITED, message) from exc
            if code in {"ValidationException", "ResourceNotFoundException"}:
                raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, message, penalize=False) from exc
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, message) from exc
        except BotoCoreError as exc:
            raise ProviderError(self.name, ProviderStatus.NETWORK_ERROR, str(exc)[:400]) from exc

        blocks = (response.get("output", {}).get("message", {}) or {}).get("content", [])
        text = "".join(block.get("text", "") for block in blocks)
        if not text.strip():
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "the model returned no text", penalize=False)
        usage = response.get("usage", {})
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(self.model),
            usage=Usage(input_tokens=int(usage.get("inputTokens", 0)), output_tokens=int(usage.get("outputTokens", 0))),
            finish_reason="length" if response.get("stopReason") == "max_tokens" else "stop",
        )
