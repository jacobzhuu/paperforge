import pytest
from storage import FilesystemObjectStore


@pytest.mark.parametrize("key", ["../escape", "/tmp/escape", "nested/../../escape"])
def test_filesystem_object_store_rejects_path_traversal(tmp_path, key):
    store = FilesystemObjectStore(str(tmp_path / "objects"))
    with pytest.raises(ValueError, match="unsafe object key"):
        store.put(key, b"nope")
