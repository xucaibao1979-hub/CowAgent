"""Public, non-sensitive revision of locally served UI assets."""
import hashlib
from pathlib import Path


def page_revision():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted([root / 'chat.html', *root.glob('static/js/*.js'), *root.glob('static/css/*.css')]):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


def stamp_page(html):
    return html.replace('{{COW_PAGE_REVISION}}', page_revision())
