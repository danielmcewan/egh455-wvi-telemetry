"""Target snapshot storage - the half of REQ-F-07 that is about pictures.

REQ-F-07 asks for "the images of the targets that are taken directly from the
UAVPayload_TAQ", updating "every time a new picture is taken". A list of class
names does not satisfy that; the operator has to be able to *look* at what the
detector saw.

The image processing subsystem gets three ways to deliver a snapshot, because
we do not yet know whether their code ends up on the same Pi as this server:

  1. write the file into ``data/targets/`` and send only its name;
  2. send the bytes inline, base64-encoded, in the detection message;
  3. POST the file to ``/api/targets/image`` and send back the name we return.

All three converge on a plain file in one directory, referenced by name. That
convergence is deliberate - the browser, the database and the gallery never
learn which route a given image took.
"""
import base64
import binascii
import re
from pathlib import Path
from typing import Optional, Tuple

TARGET_DIR = Path(__file__).resolve().parent.parent / "data" / "targets"

# Keep the gallery from eating the Pi's SD card during a long flight.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_STORED_IMAGES = 400

# Magic numbers, not file extensions. A producer can call a file anything;
# what matters is that the browser will actually render the bytes.
_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

# One path segment, no directory traversal, no surprises.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def init() -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)


def sniff_extension(data: bytes) -> Optional[str]:
    """Return the extension the bytes actually are, or None if unrecognised."""
    for magic, ext in _SIGNATURES:
        if data.startswith(magic):
            return ext
    return None


def safe_name(name: str) -> Optional[str]:
    """Reduce whatever a producer sent to a single safe file name.

    Producers send things like ``targets/det_0088.jpg`` because that is what
    the path looks like on their machine. We only ever care about the last
    segment, and we refuse anything that could climb out of the directory.
    """
    if not name:
        return None
    tail = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return tail if _SAFE_NAME.match(tail) else None


def path_for(name: str) -> Optional[Path]:
    """Resolve a stored image name to a real file, or None."""
    safe = safe_name(name)
    if not safe:
        return None
    p = (TARGET_DIR / safe).resolve()
    # Belt and braces: even with a safe name, confirm we stayed inside.
    if TARGET_DIR.resolve() not in p.parents or not p.is_file():
        return None
    return p


def save_bytes(data: bytes, stem: str) -> str:
    """Write image bytes under a name derived from `stem`. Returns the name.

    Raises ValueError if the bytes are too large or are not an image format a
    browser can display - failing loudly here is far kinder than a gallery
    full of broken image icons on demo day.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"image is {len(data)} bytes, limit is {MAX_IMAGE_BYTES}")
    ext = sniff_extension(data)
    if ext is None:
        raise ValueError("not a JPEG, PNG or GIF - check you sent raw image "
                         "bytes and not a file path or a data: URL")
    stem = safe_name(stem) or "target"
    stem = stem.rsplit(".", 1)[0][:80] or "target"
    name = f"{stem}{ext}"
    init()
    (TARGET_DIR / name).write_bytes(data)
    _prune()
    return name


def save_b64(b64: str, stem: str) -> str:
    """Same as `save_bytes` but for the inline route.

    Accepts a bare base64 string or a full ``data:image/jpeg;base64,...`` URL,
    because both are what people actually paste in.
    """
    payload = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"image_b64 is not valid base64: {exc}") from exc
    return save_bytes(data, stem)


def _prune() -> None:
    """Oldest-first deletion once the directory exceeds the cap.

    A 10-minute flight at one detection every few seconds does not come close
    to this, but an all-afternoon integration session would.
    """
    files = sorted(TARGET_DIR.glob("*"), key=lambda f: f.stat().st_mtime)
    for stale in files[:-MAX_STORED_IMAGES]:
        try:
            stale.unlink()
        except OSError:
            pass


def stored_count() -> int:
    if not TARGET_DIR.is_dir():
        return 0
    return sum(1 for f in TARGET_DIR.iterdir() if f.is_file())
