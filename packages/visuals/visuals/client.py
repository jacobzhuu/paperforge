from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx
from observability.http import post_when_available

MAX_RENDITION_BYTES = 16 * 1024 * 1024


class VisualdError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rendition:
    format: str
    data: bytes
    media_type: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class RenderResult:
    renditions: list[Rendition]
    provenance: dict[str, Any]


class VisualdClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds, trust_env=False)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def render_chart(self, spec: dict[str, Any], data: dict[str, Any]) -> list[Rendition]:
        return self.render_chart_result(spec, data).renditions

    def render_chart_result(self, spec: dict[str, Any], data: dict[str, Any]) -> RenderResult:
        return self._request("/render/chart", {"spec": spec, "data": data})

    def render_diagram(self, spec: dict[str, Any]) -> list[Rendition]:
        return self.render_diagram_result(spec).renditions

    def render_diagram_result(self, spec: dict[str, Any]) -> RenderResult:
        return self._request("/render/diagram", {"spec": spec})

    def normalize(self, image: bytes, *, target_size: str | None = None) -> list[Rendition]:
        return self.normalize_result(image, target_size=target_size).renditions

    def normalize_result(
        self,
        image: bytes,
        *,
        target_size: str | None = None,
    ) -> RenderResult:
        return self._request(
            "/normalize",
            {
                "image_base64": base64.b64encode(image).decode("ascii"),
                "target_size": target_size,
            },
        )

    def _request(self, path: str, payload: dict[str, Any]) -> RenderResult:
        try:
            response = post_when_available(
                self.client, f"{self.base_url}{path}", json=payload, timeout=self.timeout_seconds
            )
        except httpx.HTTPError as error:
            raise VisualdError(f"visuald unavailable: {type(error).__name__}") from error
        if response.status_code != 200:
            raise VisualdError(f"visuald HTTP {response.status_code}: {response.text[:300]}")
        try:
            body = response.json()
            raw = body["renditions"]
            renditions = []
            for item in raw:
                data = base64.b64decode(item["data_base64"], validate=True)
                if len(data) > MAX_RENDITION_BYTES:
                    raise VisualdError("visuald rendition exceeds 16 MiB")
                renditions.append(
                    Rendition(
                        format=item["format"],
                        data=data,
                        media_type=item["media_type"],
                        width=item.get("width"),
                        height=item.get("height"),
                    )
                )
            provenance = body.get("provenance")
            return RenderResult(
                renditions=renditions,
                provenance=provenance if isinstance(provenance, dict) else {},
            )
        except (KeyError, TypeError, ValueError) as error:
            raise VisualdError("invalid visuald response") from error
