from PIL import Image
import pytest

from common.file_evidence import inspect_file_evidence


def test_image_metadata_is_checked_without_claiming_identity(tmp_path):
    path = tmp_path / "正面n.jpg"
    Image.new("RGB", (5, 3), "white").save(path)
    result = inspect_file_evidence(path, "徐才宝身份证")
    assert result["image_decodable"]
    assert result["dimensions"] == [5, 3]
    assert result["sensitive_identity"]
    assert not result["content_verified"]


def test_corrupt_image_is_rejected_before_staging(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="无法解码"):
        inspect_file_evidence(path)
