import os

import pytest
from storage import FilesystemObjectStore, MinioObjectStore


@pytest.mark.parametrize("key", ["../escape", "/tmp/escape", "nested/../../escape"])
def test_filesystem_object_store_rejects_path_traversal(tmp_path, key):
    store = FilesystemObjectStore(str(tmp_path / "objects"))
    with pytest.raises(ValueError, match="unsafe object key"):
        store.put(key, b"nope")


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses the write permission bit")
def test_filesystem_probe_rejects_a_root_that_can_no_longer_take_writes(tmp_path):
    root = tmp_path / "objects"
    store = FilesystemObjectStore(str(root))
    store.probe()
    root.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="not writable"):
            store.probe()
    finally:
        root.chmod(0o700)


def test_minio_probe_reports_a_missing_bucket_rather_than_returning_ok():
    """探测要区分「桶没了」和「服务不可达」；后者由客户端自己抛传输错误。"""

    class _Client:
        def __init__(self, exists):
            self.exists = exists
            self.checked = []

        def bucket_exists(self, bucket):
            self.checked.append(bucket)
            return self.exists

    store = MinioObjectStore.__new__(MinioObjectStore)
    store.bucket = "paperforge"
    store.client = _Client(True)
    store.probe()
    assert store.client.checked == ["paperforge"]

    store.client = _Client(False)
    with pytest.raises(RuntimeError, match="bucket is missing"):
        store.probe()
