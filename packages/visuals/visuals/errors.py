"""视觉生成的统一错误词表。

此前每层各写各的：provider 抛 `auth` / `moderation` / `provider_5xx`，worker 对任何
非 `ImageProviderError` 直接把 `type(error).__name__` 写进 `visual.error_code`——
于是数据库里混着 `ValueError`、`ConnectError` 这类对用户毫无意义的裸类名，而
**图表与示意图占建议的大多数**，它们的失败全都走那条路。

这里定义唯一一份对外词表，并规定三件事：

1. provider 直接抛这些 code，不再有第二套内部命名；
2. worker 的 except 分支必须经 `classify_visual_error` 落码，裸类名只允许出现在
   脱敏后的 message 里；
3. 历史行里的旧 code 由 `LEGACY_CODE_ALIASES` 在读侧映射，不做破坏性数据迁移。
"""

from __future__ import annotations

from dataclasses import dataclass

PROVIDER_NOT_CONFIGURED = "provider_not_configured"
AUTHENTICATION_FAILED = "authentication_failed"
CONTENT_REJECTED = "content_rejected"
INVALID_REQUEST = "invalid_request"
RATE_LIMITED = "rate_limited"
PROVIDER_UNAVAILABLE = "provider_unavailable"
NETWORK_TIMEOUT = "network_timeout"
INVALID_IMAGE = "invalid_image"
NORMALIZATION_FAILED = "normalization_failed"
VISUALD_UNAVAILABLE = "visuald_unavailable"
SOURCE_UNRESOLVED = "source_unresolved"
INTERNAL_ERROR = "internal_error"

VISUAL_ERROR_CODES: frozenset[str] = frozenset(
    {
        PROVIDER_NOT_CONFIGURED,
        AUTHENTICATION_FAILED,
        CONTENT_REJECTED,
        INVALID_REQUEST,
        RATE_LIMITED,
        PROVIDER_UNAVAILABLE,
        NETWORK_TIMEOUT,
        INVALID_IMAGE,
        NORMALIZATION_FAILED,
        VISUALD_UNAVAILABLE,
        SOURCE_UNRESOLVED,
        INTERNAL_ERROR,
    }
)

#: 旧值 → 新值。仅用于**读**历史 `visual.error_code`；不回写数据库。
LEGACY_CODE_ALIASES: dict[str, str] = {
    "auth": AUTHENTICATION_FAILED,
    "moderation": CONTENT_REJECTED,
    "rate_limit": RATE_LIMITED,
    "provider_5xx": PROVIDER_UNAVAILABLE,
    "provider_error": PROVIDER_UNAVAILABLE,
    "network": NETWORK_TIMEOUT,
    "invalid_base64": INVALID_IMAGE,
    "image_too_large": INVALID_IMAGE,
    "invalid_image_type": INVALID_IMAGE,
    "visuals_disabled": PROVIDER_NOT_CONFIGURED,
    "ai_images_disabled": PROVIDER_NOT_CONFIGURED,
}

#: 每个 code 的用户可执行提示。文案回答的是「我现在该做什么」，
#: 而不是复述厂商的英文错误串。
ERROR_MESSAGES: dict[str, str] = {
    PROVIDER_NOT_CONFIGURED: "图像服务尚未配置或已关闭。在设置中确认提供商与凭据后再生成。",
    AUTHENTICATION_FAILED: "图像服务拒绝了当前凭据。检查 API Token 是否有效、是否过期或权限不足。",
    CONTENT_REJECTED: "提示词可能触发了图像服务的内容检查。改写描述（避开安全敏感词组合）后重试。",
    INVALID_REQUEST: "图像服务不接受当前请求参数。检查提示词长度与规格设置后重试。",
    RATE_LIMITED: "图像服务正在限流。稍等片刻再重试，不必修改提示词。",
    PROVIDER_UNAVAILABLE: "图像服务暂时不可用。稍后重试；若持续失败请检查服务商状态页。",
    NETWORK_TIMEOUT: "连接图像服务超时。检查网络或代理设置后重试。",
    INVALID_IMAGE: "图像服务返回的内容不是可用的图片。重试一次；若持续出现请更换模型。",
    NORMALIZATION_FAILED: "图片归一化失败。重试一次；若持续失败请检查 visuald 服务日志。",
    VISUALD_UNAVAILABLE: "图形渲染服务（visuald）不可用。确认该服务已启动后重试。",
    SOURCE_UNRESOLVED: (
        "图表的数据来源无法解析：素材可能已被删除，或尚未完成解析。回素材中心确认后重试。"
    ),
    INTERNAL_ERROR: "生成过程中发生未预期的错误。重试一次；若持续失败请附带下方错误摘要反馈。",
}

#: 重试有意义的 code。界面据此决定是给「重试」还是给「先改提示词」。
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        RATE_LIMITED,
        PROVIDER_UNAVAILABLE,
        NETWORK_TIMEOUT,
        NORMALIZATION_FAILED,
        VISUALD_UNAVAILABLE,
        INTERNAL_ERROR,
    }
)


@dataclass(frozen=True)
class VisualErrorInfo:
    """一次失败的完整对外描述。"""

    code: str
    message: str
    retryable: bool
    request_id: str | None = None
    #: 脱敏后的技术细节（异常类名 + 截断的原始文本），供排查而非展示给用户当主文案。
    detail: str | None = None


def normalize_code(code: str | None) -> str:
    """把任意来源的 code 归一到词表内。未知值一律归 internal_error。"""
    if not code:
        return INTERNAL_ERROR
    if code in VISUAL_ERROR_CODES:
        return code
    return LEGACY_CODE_ALIASES.get(code, INTERNAL_ERROR)


def message_for(code: str) -> str:
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES[INTERNAL_ERROR])


def is_retryable(code: str) -> bool:
    return code in RETRYABLE_CODES


def classify_visual_error(error: BaseException) -> VisualErrorInfo:
    """把任意异常映射成对外的 `VisualErrorInfo`。

    禁止让裸异常类名流到 `visual.error_code`：类名进 `detail`，code 走词表。
    """
    # 延迟导入：errors 模块被 provider/client 反向依赖，顶层导入会成环。
    from visuals.client import VisualdError
    from visuals.provider import ImageProviderError

    detail = f"{type(error).__name__}: {error}"[:300]

    if isinstance(error, ImageProviderError):
        code = normalize_code(error.code)
        return VisualErrorInfo(
            code=code,
            message=message_for(code),
            # provider 自己判定的可重试性更准确（它知道自己重试过几次）。
            retryable=error.retryable,
            request_id=error.request_id,
            detail=detail,
        )

    if isinstance(error, VisualdError):
        # visuald 既负责确定性渲染，也负责 AI 图的归一化。两条路径的失败
        # 对用户的含义不同，靠消息里的 `/normalize` 区分。
        code = NORMALIZATION_FAILED if "normalize" in str(error) else VISUALD_UNAVAILABLE
        return VisualErrorInfo(
            code=code, message=message_for(code), retryable=True, detail=detail
        )

    if isinstance(error, _SOURCE_ERRORS) and _looks_like_source_failure(error):
        return VisualErrorInfo(
            code=SOURCE_UNRESOLVED,
            message=message_for(SOURCE_UNRESOLVED),
            retryable=False,
            detail=detail,
        )

    return VisualErrorInfo(
        code=INTERNAL_ERROR,
        message=message_for(INTERNAL_ERROR),
        retryable=True,
        detail=detail,
    )


_SOURCE_ERRORS = (ValueError, FileNotFoundError)
_SOURCE_MARKERS = (
    "asset",
    "source",
    "not parsed",
    "does not resolve",
)


def _looks_like_source_failure(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _SOURCE_MARKERS)
