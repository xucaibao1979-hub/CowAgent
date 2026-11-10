"""Verify local media metadata without inferring a person's identity."""
from __future__ import annotations

from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def inspect_file_evidence(path: str | Path, task_hint: str = "") -> dict:
    candidate = Path(path).resolve(strict=True)
    if not candidate.is_file():
        raise ValueError("目标不是文件")
    size = candidate.stat().st_size
    if size <= 0:
        raise ValueError("文件为空")
    result = {"name": candidate.name, "size": size, "extension": candidate.suffix.lower(),
              "image_decodable": None, "content_verified": False,
              "sensitive_identity": any(word in (str(candidate) + str(task_hint))
                                        for word in ("身份证", "证件照", "正面", "反面"))}
    if candidate.suffix.lower() in IMAGE_EXTENSIONS:
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(candidate) as image:
                image.verify()
            with Image.open(candidate) as image:
                result["dimensions"] = [image.width, image.height]
                result["format"] = image.format
            result["image_decodable"] = True
        except (OSError, UnidentifiedImageError) as error:
            raise ValueError("图片文件无法解码，不能按照片发送") from error
    return result
