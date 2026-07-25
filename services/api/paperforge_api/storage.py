"""对象存储 seam 的兼容再导出。

实现已提取到 `packages/storage`，因为 API 与 worker 都要用它
（worker 导出产物、API 提供下载）；把实现留在 API 包里会造成包循环依赖。
"""

from storage import FilesystemObjectStore, ObjectStore, content_key

__all__ = ["FilesystemObjectStore", "ObjectStore", "content_key"]
