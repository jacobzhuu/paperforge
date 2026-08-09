from __future__ import annotations

import base64
import ipaddress
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from visuals.errors import (
    AUTHENTICATION_FAILED,
    CONTENT_REJECTED,
    INVALID_IMAGE,
    INVALID_REQUEST,
    NETWORK_TIMEOUT,
    PROVIDER_NOT_CONFIGURED,
    PROVIDER_UNAVAILABLE,
    RATE_LIMITED,
)

MAX_PROVIDER_IMAGE_BYTES = 32 * 1024 * 1024
_CLOUDFLARE_ACCOUNT_ID = re.compile(r"^[a-fA-F0-9]{32}$")


@dataclass(frozen=True)
class ImageProviderCapabilities:
    """提供商**真正**支持的能力。

    界面不能假设所有提供商能力相同：Cloudflare FLUX.1-schnell 的请求体支持
    `prompt`、`steps` 与 `seed`，但 `size` 根本不会被发送——此前界面却提供「横向 3:2 /
    方形 1:1 / 竖向 2:3」三选一，用户选了横向仍然拿到 1024×1024。

    工作台按这份声明渲染表单，因此新增提供商不需要在 UI 里加厂商分支。
    """

    provider: str
    model: str
    #: 可请求的精确尺寸。空表示尺寸由提供商决定，界面不得给出尺寸承诺。
    supported_sizes: tuple[str, ...] = ()
    supported_aspect_ratios: tuple[str, ...] = ()
    quality_modes: tuple[str, ...] = ("low", "medium", "high")
    prompt_max_length: int = 4000
    supports_negative_prompt: bool = False
    supports_seed: bool = False
    #: 输出尺寸固定时填这里（如 Cloudflare 的方形输出），界面据此显示「固定」。
    fixed_output_size: str | None = None
    cost_estimate_available: bool = False
    #: 一句话解释这个提供商的取舍，直接展示在生成确认框里。
    note: str | None = None


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    size: str = "1536x1024"
    quality: str = "medium"
    output_format: str = "png"
    negative_prompt: str | None = None
    seed: int | None = None


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
    """`code` 取自 `visuals.errors` 的统一词表。

    `request_id` 此前只在**成功**路径上被读取（`ImageResult.request_id`），失败时
    恒为 None——排查一次 Cloudflare 拒绝时拿不到 cf-ray，用户只能反复试提示词。
    现在失败路径也带上它。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


def _request_id_of(response: httpx.Response | None) -> str | None:
    if response is None:
        return None
    return (
        response.headers.get("cf-ray")
        or response.headers.get("x-request-id")
        or response.headers.get("x-amzn-requestid")
    )


class ImageProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    @property
    def capabilities(self) -> ImageProviderCapabilities: ...

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
        provider_name: str = "openai",
        accept_image_urls: bool = False,
        include_output_format: bool = True,
        supports_negative_prompt: bool = False,
        supports_seed: bool = False,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(max_retries, 4))
        self.provider_name = provider_name
        self.accept_image_urls = accept_image_urls
        self.include_output_format = include_output_format
        self.supports_negative_prompt = supports_negative_prompt
        self.supports_seed = supports_seed
        self._client = client
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def capabilities(self) -> ImageProviderCapabilities:
        return ImageProviderCapabilities(
            provider=self.provider_name,
            model=self.model,
            supported_sizes=("1024x1024", "1536x1024", "1024x1536"),
            supported_aspect_ratios=("1:1", "3:2", "2:3"),
            quality_modes=("low", "medium", "high"),
            prompt_max_length=4000,
            supports_negative_prompt=self.supports_negative_prompt,
            supports_seed=self.supports_seed,
            cost_estimate_available=False,
            note="尺寸与质量都会真实下发到 OpenAI Image API。",
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
        if not self.api_key:
            raise ImageProviderError(
                "image API key is not configured",
                code=PROVIDER_NOT_CONFIGURED,
                retryable=False,
            )
        _validate_optional_request_capabilities(request, self.capabilities)
        payload = {
            "model": self.model,
            "prompt": request.prompt,
            "size": request.size,
            "quality": request.quality,
            "n": 1,
        }
        if self.include_output_format:
            payload["output_format"] = request.output_format
        if request.negative_prompt is not None:
            payload["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            payload["seed"] = request.seed
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
                        type(error).__name__, code=NETWORK_TIMEOUT, retryable=True
                    ) from error
                time.sleep(0.25 * (2**attempt))
                continue
            request_id = _request_id_of(response)
            if response.status_code in {401, 403}:
                raise ImageProviderError(
                    "image provider authentication failed",
                    code=AUTHENTICATION_FAILED,
                    retryable=False,
                    request_id=request_id,
                )
            if response.status_code in {400, 422}:
                body = response.text.lower()
                code = (
                    CONTENT_REJECTED
                    if "moderation" in body or "safety" in body
                    else INVALID_REQUEST
                )
                raise ImageProviderError(
                    "image request rejected",
                    code=code,
                    retryable=False,
                    request_id=request_id,
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise ImageProviderError(
                    f"image provider HTTP {response.status_code}",
                    code=RATE_LIMITED if response.status_code == 429 else PROVIDER_UNAVAILABLE,
                    retryable=True,
                    request_id=request_id,
                )
            if response.status_code != 200:
                raise ImageProviderError(
                    f"image provider HTTP {response.status_code}",
                    code=PROVIDER_UNAVAILABLE,
                    retryable=False,
                    request_id=request_id,
                )
            break
        if response is None:
            raise ImageProviderError(
                "image provider returned no response", code=NETWORK_TIMEOUT, retryable=True
            )
        request_id = _request_id_of(response)
        try:
            body = response.json()
            image = body["data"][0]
            if not isinstance(image, dict):
                raise TypeError("image result must be an object")
            encoded = image.get("b64_json")
            image_url = image.get("url")
            if isinstance(encoded, str):
                data = base64.b64decode(encoded, validate=True)
            elif self.accept_image_urls and isinstance(image_url, str):
                data = self._download_image(image_url, request_id=request_id)
            else:
                raise KeyError("image result has neither b64_json nor an accepted URL")
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ImageProviderError(
                "invalid image provider payload",
                code=INVALID_IMAGE,
                retryable=False,
                request_id=request_id,
            ) from error
        if not data or len(data) > MAX_PROVIDER_IMAGE_BYTES:
            raise ImageProviderError(
                "provider image exceeds size limit",
                code=INVALID_IMAGE,
                retryable=False,
                request_id=request_id,
            )
        media_type = _image_media_type(data)
        if media_type is None:
            raise ImageProviderError(
                "provider returned an unsupported image payload",
                code=INVALID_IMAGE,
                retryable=False,
                request_id=request_id,
            )
        if (
            not self.accept_image_urls
            and request.output_format == "png"
            and media_type != "image/png"
        ):
            raise ImageProviderError(
                "provider returned a non-PNG payload",
                code=INVALID_IMAGE,
                retryable=False,
                request_id=request_id,
            )
        return ImageResult(
            data=data,
            media_type=media_type,
            provider=self.provider_name,
            model=self.model,
            request_id=request_id,
            usage=body.get("usage") if isinstance(body.get("usage"), dict) else {},
        )

    def _download_image(self, image_url: str, *, request_id: str | None) -> bytes:
        """下载 provider 返回的临时 URL，不把 API key 转发给图床。

        Yunwu 聚合的模型并不统一：GPT Image 常返回 Base64，而 DALL·E、FLUX、
        Seedream 等常返回带签名的 HTTPS URL。生成请求可能已经计费，所以下载失败
        时只重试同一个 URL，绝不重新提交生图请求。
        """
        _validate_provider_image_url(image_url, request_id=request_id)
        for attempt in range(self.max_retries + 1):
            try:
                with self.client.stream(
                    "GET",
                    image_url,
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < self.max_retries:
                            time.sleep(0.25 * (2**attempt))
                            continue
                        raise ImageProviderError(
                            f"image download HTTP {response.status_code}",
                            code=(
                                RATE_LIMITED
                                if response.status_code == 429
                                else PROVIDER_UNAVAILABLE
                            ),
                            retryable=True,
                            request_id=request_id,
                        )
                    if response.status_code != 200:
                        raise ImageProviderError(
                            f"image download HTTP {response.status_code}",
                            code=PROVIDER_UNAVAILABLE,
                            retryable=False,
                            request_id=request_id,
                        )
                    declared_size = response.headers.get("content-length")
                    if declared_size and int(declared_size) > MAX_PROVIDER_IMAGE_BYTES:
                        raise ImageProviderError(
                            "provider image exceeds size limit",
                            code=INVALID_IMAGE,
                            retryable=False,
                            request_id=request_id,
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_PROVIDER_IMAGE_BYTES:
                            raise ImageProviderError(
                                "provider image exceeds size limit",
                                code=INVALID_IMAGE,
                                retryable=False,
                                request_id=request_id,
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            except ImageProviderError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self.max_retries:
                    raise ImageProviderError(
                        type(error).__name__,
                        code=NETWORK_TIMEOUT,
                        retryable=True,
                        request_id=request_id,
                    ) from error
                time.sleep(0.25 * (2**attempt))
            except ValueError as error:
                raise ImageProviderError(
                    "invalid image download response",
                    code=INVALID_IMAGE,
                    retryable=False,
                    request_id=request_id,
                ) from error
        raise ImageProviderError(
            "image download returned no response",
            code=NETWORK_TIMEOUT,
            retryable=True,
            request_id=request_id,
        )


class YunwuImageProvider(OpenAIImageProvider):
    """Yunwu 的 OpenAI-compatible 生图接口。

    Yunwu 下游模型的响应并不完全一致，因此同时接受 `b64_json` 与 HTTPS URL；
    URL 图片下载后仍会做大小与 magic bytes 校验，再交给 visuald 规范化。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-image-1",
        base_url: str = "https://yunwu.ai/v1",
        timeout_seconds: float = 180.0,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            client=client,
            provider_name="yunwu",
            accept_image_urls=True,
            # Yunwu 的 GPT Image 文档使用最小 OpenAI 请求体；部分聚合模型会拒绝
            # OpenAI 新增的 output_format 字段，但都能由返回内容判断实际格式。
            include_output_format=False,
        )

    @property
    def capabilities(self) -> ImageProviderCapabilities:
        return ImageProviderCapabilities(
            provider="yunwu",
            model=self.model,
            supported_sizes=("1024x1024", "1536x1024", "1024x1536"),
            supported_aspect_ratios=("1:1", "3:2", "2:3"),
            quality_modes=("low", "medium", "high"),
            prompt_max_length=4000,
            # 默认 gpt-image-1 走 OpenAI-compatible Images API；该 API 当前没有
            # negative_prompt / seed 字段。聚合接口不能因为某些下游模型可能接受
            # 就对所有 Yunwu 模型盲目透传。
            supports_negative_prompt=False,
            supports_seed=False,
            cost_estimate_available=False,
            note="通过 Yunwu OpenAI-compatible Images API 生成，支持 Base64 或临时 URL 返回。",
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip() and self.model.strip() and self.base_url.strip())


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
    def capabilities(self) -> ImageProviderCapabilities:
        """FLUX.1-schnell 的请求体支持 `prompt`、`steps` 与 `seed`。

        因此这里**不声明任何可选尺寸**：界面据此显示「输出尺寸由提供商决定」，
        而不是给出一个不会生效的 3:2 / 1:1 / 2:3 下拉框。质量三档是真实的——
        它映射到 4 / 6 / 8 步。
        """
        return ImageProviderCapabilities(
            provider="cloudflare",
            model=self.model,
            supported_sizes=(),
            supported_aspect_ratios=(),
            quality_modes=("low", "medium", "high"),
            prompt_max_length=2048,
            supports_negative_prompt=False,
            # Cloudflare 的 FLUX.1-schnell 官方输入示例与 schema 接受 seed；
            # 其他自定义 model 不能沿用该承诺。
            supports_seed=self.model == "@cf/black-forest-labs/flux-1-schnell",
            fixed_output_size=None,
            cost_estimate_available=False,
            note=(
                "Cloudflare Workers AI 接受提示词、步数与随机种子；"
                "输出尺寸由模型决定，生成后会显示实际尺寸。"
            ),
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
                "Cloudflare API token is not configured",
                code=PROVIDER_NOT_CONFIGURED,
                retryable=False,
            )
        if not _CLOUDFLARE_ACCOUNT_ID.fullmatch(self.account_id):
            raise ImageProviderError(
                "Cloudflare Account ID is not configured",
                code=PROVIDER_NOT_CONFIGURED,
                retryable=False,
            )
        if len(request.prompt) > 2048:
            raise ImageProviderError(
                "Cloudflare FLUX prompt exceeds 2048 characters",
                code=INVALID_REQUEST,
                retryable=False,
            )
        if not self.model:
            raise ImageProviderError(
                "Cloudflare image model is not configured",
                code=PROVIDER_NOT_CONFIGURED,
                retryable=False,
            )
        _validate_optional_request_capabilities(request, self.capabilities)

        steps = {"low": 4, "medium": 6, "high": 8}.get(request.quality, 6)
        payload = {"prompt": request.prompt, "steps": steps}
        if request.seed is not None:
            payload["seed"] = request.seed
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
                        type(error).__name__, code=NETWORK_TIMEOUT, retryable=True
                    ) from error
                time.sleep(0.25 * (2**attempt))
                continue
            # cf-ray 此前只在成功路径上被读取，排查一次拒绝时拿不到任何追踪 ID。
            request_id = _request_id_of(response)
            if response.status_code in {401, 403}:
                raise ImageProviderError(
                    "Cloudflare authentication failed",
                    code=AUTHENTICATION_FAILED,
                    retryable=False,
                    request_id=request_id,
                )
            if response.status_code in {400, 422}:
                body = response.text.lower()
                code = (
                    CONTENT_REJECTED
                    if "moderation" in body or "safety" in body or "content policy" in body
                    else INVALID_REQUEST
                )
                raise ImageProviderError(
                    "Cloudflare image request rejected",
                    code=code,
                    retryable=False,
                    request_id=request_id,
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise ImageProviderError(
                    f"Cloudflare Workers AI HTTP {response.status_code}",
                    code=RATE_LIMITED if response.status_code == 429 else PROVIDER_UNAVAILABLE,
                    retryable=True,
                    request_id=request_id,
                )
            if response.status_code != 200:
                raise ImageProviderError(
                    f"Cloudflare Workers AI HTTP {response.status_code}",
                    code=PROVIDER_UNAVAILABLE,
                    retryable=False,
                    request_id=request_id,
                )
            break

        if response is None:
            raise ImageProviderError(
                "Cloudflare returned no response", code=NETWORK_TIMEOUT, retryable=True
            )
        request_id = _request_id_of(response)
        data, api_usage = _cloudflare_image_data(response, request_id)
        if not data or len(data) > MAX_PROVIDER_IMAGE_BYTES:
            raise ImageProviderError(
                "provider image exceeds size limit",
                code=INVALID_IMAGE,
                retryable=False,
                request_id=request_id,
            )
        media_type = _image_media_type(data)
        if media_type is None:
            raise ImageProviderError(
                "Cloudflare returned an unsupported image payload",
                code=INVALID_IMAGE,
                retryable=False,
                request_id=request_id,
            )
        usage = {
            "steps": steps,
            "requested_quality": request.quality,
            # 请求里的 size 从未下发给 Cloudflare，只作为「用户当时想要什么」留档。
            "requested_size": request.size,
            **({"seed": request.seed} if request.seed is not None else {}),
            **api_usage,
        }
        return ImageResult(
            data=data,
            media_type=media_type,
            provider="cloudflare",
            model=self.model,
            request_id=request_id,
            usage=usage,
        )


def _validate_optional_request_capabilities(
    request: ImageRequest,
    capabilities: ImageProviderCapabilities,
) -> None:
    """拒绝不受支持的可选参数，避免“界面能填、服务端悄悄丢”的假能力。"""
    if request.negative_prompt is not None and not capabilities.supports_negative_prompt:
        raise ImageProviderError(
            f"{capabilities.provider} image provider does not support negative_prompt",
            code=INVALID_REQUEST,
            retryable=False,
        )
    if request.seed is not None and not capabilities.supports_seed:
        raise ImageProviderError(
            f"{capabilities.provider} image provider does not support seed",
            code=INVALID_REQUEST,
            retryable=False,
        )


def _cloudflare_image_data(
    response: httpx.Response, request_id: str | None = None
) -> tuple[bytes, dict[str, Any]]:
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
                code=PROVIDER_UNAVAILABLE,
                retryable=False,
                request_id=request_id,
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
            "invalid Cloudflare image payload",
            code=INVALID_IMAGE,
            retryable=False,
            request_id=request_id,
        ) from error


def _image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _validate_provider_image_url(image_url: str, *, request_id: str | None = None) -> None:
    """只接受无用户信息的公网 HTTPS URL，挡住明显的 SSRF 目标。"""
    try:
        parsed = urlsplit(image_url)
        hostname = parsed.hostname
        if (
            parsed.scheme.lower() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("image URL must be an HTTPS URL without user info")
        normalized_host = hostname.rstrip(".").lower()
        if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
            raise ValueError("localhost is not an accepted image host")
        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("image URL must not target a private or reserved address")
    except ValueError as error:
        raise ImageProviderError(
            "provider returned an unsafe image URL",
            code=INVALID_IMAGE,
            retryable=False,
            request_id=request_id,
        ) from error


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
            code=PROVIDER_NOT_CONFIGURED,
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


def image_provider_capabilities(config: ImageProviderConfig) -> ImageProviderCapabilities | None:
    """未配置或不支持的 provider 返回 None——界面据此完全隐藏 AI 生图表单。

    **不接触任何凭据**：返回值里只有能力，没有 Token / Account ID / Base URL。
    """
    try:
        provider = create_image_provider(config)
    except ImageProviderError:
        return None
    try:
        return provider.capabilities
    finally:
        provider.close()


def _openai_factory(config: ImageProviderConfig, client: httpx.Client | None) -> ImageProvider:
    return OpenAIImageProvider(
        api_key=config.api_key,
        model=config.model or "gpt-image-2",
        base_url=config.base_url or "https://api.openai.com/v1",
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        client=client,
    )


def _cloudflare_factory(config: ImageProviderConfig, client: httpx.Client | None) -> ImageProvider:
    return CloudflareWorkersAIImageProvider(
        account_id=config.account_id,
        api_key=config.api_key,
        model=config.model or "@cf/black-forest-labs/flux-1-schnell",
        base_url=config.base_url or "https://api.cloudflare.com/client/v4",
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        client=client,
    )


def _yunwu_factory(config: ImageProviderConfig, client: httpx.Client | None) -> ImageProvider:
    return YunwuImageProvider(
        api_key=config.api_key,
        model=config.model or "gpt-image-1",
        base_url=config.base_url or "https://yunwu.ai/v1",
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        client=client,
    )


register_image_provider("openai", _openai_factory)
register_image_provider("cloudflare", _cloudflare_factory)
register_image_provider("yunwu", _yunwu_factory)
