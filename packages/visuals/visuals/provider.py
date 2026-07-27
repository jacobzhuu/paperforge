from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

MAX_PROVIDER_IMAGE_BYTES = 32 * 1024 * 1024
_CLOUDFLARE_ACCOUNT_ID = re.compile(r"^[a-fA-F0-9]{32}$")


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    size: str = "1536x1024"
    quality: str = "medium"
    output_format: str = "png"


@dataclass(frozen=True)
class ImageResult:
    data: bytes
    media_type: str
    provider: str
    model: str
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageProviderConfig:
    """厂商无关的图像 provider 配置；业务管线只把它交给 factory。"""

    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    account_id: str = ""
    timeout_seconds: float = 180.0
    max_retries: int = 2


class ImageProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ImageProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    def generate(self, request: ImageRequest) -> ImageResult: ...

    def close(self) -> None: ...


class OpenAIImageProvider:
    """OpenAI Image API 单 prompt 生成适配器；与文本模型配置完全分离。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-image-2",
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 180.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(max_retries, 4))
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def generate(self, request: ImageRequest) -> ImageResult:
        if not self.api_key:
            raise ImageProviderError(
                "image API key is not configured", code="auth", retryable=False
            )
        payload = {
            "model": self.model,
            "prompt": request.prompt,
            "size": request.size,
            "quality": request.quality,
            "output_format": request.output_format,
            "n": 1,
        }
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/images/generations",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self.max_retries:
                    raise ImageProviderError(
                        type(error).__name__, code="network", retryable=True
                    ) from error
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code in {401, 403}:
                raise ImageProviderError(
                    "image provider authentication failed", code="auth", retryable=False
                )
            if response.status_code in {400, 422}:
                body = response.text.lower()
                code = (
                    "moderation" if "moderation" in body or "safety" in body else "invalid_request"
                )
                raise ImageProviderError("image request rejected", code=code, retryable=False)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise ImageProviderError(
                    f"image provider HTTP {response.status_code}",
                    code="rate_limit" if response.status_code == 429 else "provider_5xx",
                    retryable=True,
                )
            if response.status_code != 200:
                raise ImageProviderError(
                    f"image provider HTTP {response.status_code}",
                    code="provider_error",
                    retryable=False,
                )
            break
        if response is None:
            raise ImageProviderError(
                "image provider returned no response", code="network", retryable=True
            )
        try:
            body = response.json()
            encoded = body["data"][0]["b64_json"]
            data = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ImageProviderError(
                "invalid image provider payload", code="invalid_base64", retryable=False
            ) from error
        if not data or len(data) > MAX_PROVIDER_IMAGE_BYTES:
            raise ImageProviderError(
                "provider image exceeds size limit", code="image_too_large", retryable=False
            )
        if request.output_format == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ImageProviderError(
                "provider returned a non-PNG payload",
                code="invalid_image_type",
                retryable=False,
            )
        return ImageResult(
            data=data,
            media_type="image/png",
            provider="openai",
            model=self.model,
            request_id=response.headers.get("x-request-id"),
            usage=body.get("usage") if isinstance(body.get("usage"), dict) else {},
        )


class CloudflareWorkersAIImageProvider:
    """Cloudflare Workers AI 文生图适配器，首版面向 FLUX.1-schnell。"""

    def __init__(
        self,
        *,
        account_id: str,
        api_key: str,
        model: str = "@cf/black-forest-labs/flux-1-schnell",
        base_url: str = "https://api.cloudflare.com/client/v4",
        timeout_seconds: float = 180.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self.account_id = account_id.strip()
        self.api_key = api_key
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(max_retries, 4))
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(
            self.api_key.strip()
            and _CLOUDFLARE_ACCOUNT_ID.fullmatch(self.account_id)
            and self.model
            and self.base_url
        )

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def generate(self, request: ImageRequest) -> ImageResult:
        if not self.api_key.strip():
            raise ImageProviderError(
                "Cloudflare API token is not configured", code="auth", retryable=False
            )
        if not _CLOUDFLARE_ACCOUNT_ID.fullmatch(self.account_id):
            raise ImageProviderError(
                "Cloudflare Account ID is not configured",
                code="provider_not_configured",
                retryable=False,
            )
        if len(request.prompt) > 2048:
            raise ImageProviderError(
                "Cloudflare FLUX prompt exceeds 2048 characters",
                code="invalid_request",
                retryable=False,
            )
        if not self.model:
            raise ImageProviderError(
                "Cloudflare image model is not configured",
                code="provider_not_configured",
                retryable=False,
            )

        steps = {"low": 4, "medium": 6, "high": 8}.get(request.quality, 6)
        payload = {"prompt": request.prompt, "steps": steps}
        account_id = quote(self.account_id, safe="")
        model = quote(self.model, safe="@/-")
        url = f"{self.base_url}/accounts/{account_id}/ai/run/{model}"
        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self.max_retries:
                    raise ImageProviderError(
                        type(error).__name__, code="network", retryable=True
                    ) from error
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code in {401, 403}:
                raise ImageProviderError(
                    "Cloudflare authentication failed", code="auth", retryable=False
                )
            if response.status_code in {400, 422}:
                body = response.text.lower()
                code = (
                    "moderation"
                    if "moderation" in body or "safety" in body or "content policy" in body
                    else "invalid_request"
                )
                raise ImageProviderError(
                    "Cloudflare image request rejected", code=code, retryable=False
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise ImageProviderError(
                    f"Cloudflare Workers AI HTTP {response.status_code}",
                    code="rate_limit" if response.status_code == 429 else "provider_5xx",
                    retryable=True,
                )
            if response.status_code != 200:
                raise ImageProviderError(
                    f"Cloudflare Workers AI HTTP {response.status_code}",
                    code="provider_error",
                    retryable=False,
                )
            break

        if response is None:
            raise ImageProviderError(
                "Cloudflare returned no response", code="network", retryable=True
            )
        data, api_usage = _cloudflare_image_data(response)
        if not data or len(data) > MAX_PROVIDER_IMAGE_BYTES:
            raise ImageProviderError(
                "provider image exceeds size limit", code="image_too_large", retryable=False
            )
        media_type = _image_media_type(data)
        if media_type is None:
            raise ImageProviderError(
                "Cloudflare returned an unsupported image payload",
                code="invalid_image_type",
                retryable=False,
            )
        usage = {
            "steps": steps,
            "requested_quality": request.quality,
            "requested_size": request.size,
            **api_usage,
        }
        return ImageResult(
            data=data,
            media_type=media_type,
            provider="cloudflare",
            model=self.model,
            request_id=response.headers.get("cf-ray") or response.headers.get("x-request-id"),
            usage=usage,
        )


def _cloudflare_image_data(response: httpx.Response) -> tuple[bytes, dict[str, Any]]:
    content_type = response.headers.get("content-type", "").partition(";")[0].lower()
    if _image_media_type(response.content) is not None:
        return response.content, {}
    if content_type in {"image/png", "image/jpeg"}:
        return response.content, {}
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("success") is False:
            raise ImageProviderError(
                "Cloudflare returned an unsuccessful response",
                code="provider_error",
                retryable=False,
            )
        result = body.get("result", body)
        if isinstance(result, dict):
            encoded = result.get("image")
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        elif isinstance(result, str):
            encoded = result
            usage = {}
        else:
            raise TypeError("unexpected Cloudflare result")
        if not isinstance(encoded, str):
            raise TypeError("Cloudflare image is missing")
        return base64.b64decode(encoded, validate=True), usage
    except ImageProviderError:
        raise
    except (TypeError, ValueError, httpx.DecodingError) as error:
        raise ImageProviderError(
            "invalid Cloudflare image payload", code="invalid_base64", retryable=False
        ) from error


def _image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


ImageProviderFactory = Callable[[ImageProviderConfig, httpx.Client | None], ImageProvider]
_IMAGE_PROVIDER_FACTORIES: dict[str, ImageProviderFactory] = {}


def register_image_provider(name: str, factory: ImageProviderFactory) -> None:
    """注册图像 provider；worker 无需感知具体厂商。"""
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("image provider name cannot be empty")
    _IMAGE_PROVIDER_FACTORIES[normalized] = factory


def available_image_providers() -> tuple[str, ...]:
    return tuple(sorted(_IMAGE_PROVIDER_FACTORIES))


def create_image_provider(
    config: ImageProviderConfig,
    *,
    client: httpx.Client | None = None,
) -> ImageProvider:
    name = config.provider.strip().lower()
    factory = _IMAGE_PROVIDER_FACTORIES.get(name)
    if factory is None:
        raise ImageProviderError(
            f"unsupported image provider: {name or '<empty>'}",
            code="provider_not_configured",
            retryable=False,
        )
    return factory(config, client)


def image_provider_configured(config: ImageProviderConfig) -> bool:
    try:
        provider = create_image_provider(config)
    except ImageProviderError:
        return False
    try:
        return provider.configured
    finally:
        provider.close()


def _openai_factory(
    config: ImageProviderConfig, client: httpx.Client | None
) -> ImageProvider:
    return OpenAIImageProvider(
        api_key=config.api_key,
        model=config.model or "gpt-image-2",
        base_url=config.base_url or "https://api.openai.com/v1",
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        client=client,
    )


def _cloudflare_factory(
    config: ImageProviderConfig, client: httpx.Client | None
) -> ImageProvider:
    return CloudflareWorkersAIImageProvider(
        account_id=config.account_id,
        api_key=config.api_key,
        model=config.model or "@cf/black-forest-labs/flux-1-schnell",
        base_url=config.base_url or "https://api.cloudflare.com/client/v4",
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        client=client,
    )


register_image_provider("openai", _openai_factory)
register_image_provider("cloudflare", _cloudflare_factory)
