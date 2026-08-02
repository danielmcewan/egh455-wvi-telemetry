"""Camera feed as MJPEG - REQ-F-07, the live-stream half.

MJPEG = "Motion JPEG": just a stream of ordinary JPEG images sent one after
another down a single connection. Browsers render it natively in an <img> tag,
so there is no video player, no codec and no JavaScript involved.

Until the IP subsystem's real camera exists, this draws synthetic frames with a
fake detection box so the pane is never empty. Swap `SyntheticSource` for a real
one later; nothing else in the app changes.
"""
import io
import math
import time
from datetime import timezone
from typing import Iterator, Optional

from PIL import Image, ImageDraw

from .hub import hub
from .models import utcnow

WIDTH, HEIGHT = 640, 480
FPS = 10
BOUNDARY = "frameboundary"


class SyntheticSource:
    """Placeholder frames. Not a requirement - just stops the pane being blank."""

    def frame(self) -> bytes:
        img = Image.new("RGB", (WIDTH, HEIGHT), (238, 240, 243))
        d = ImageDraw.Draw(img)

        for x in range(0, WIDTH, 40):
            d.line([(x, 0), (x, HEIGHT)], fill=(226, 229, 234))
        for y in range(0, HEIGHT, 40):
            d.line([(0, y), (WIDTH, y)], fill=(226, 229, 234))

        t = time.time()
        cx = int(WIDTH / 2 + math.sin(t * 0.7) * 170)
        cy = int(HEIGHT / 2 + math.cos(t * 0.5) * 110)
        w = h = 120

        # Draw the newest real detection's label if one has arrived, so the pane
        # reflects actual data rather than pure fiction.
        label = "no camera attached"
        colour = (154, 103, 0)          # amber, matches the dashboard warn colour
        if hub.targets:
            newest = hub.targets[0]["data"]
            cls = newest.get("class") or newest.get("cls") or "?"
            conf = newest.get("confidence")
            label = f"{cls} {conf:.2f}" if isinstance(conf, float) else str(cls)
            colour = (31, 56, 100)      # accent navy

        d.rectangle([cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2],
                    outline=colour, width=2)
        d.text((cx - w // 2, cy - h // 2 - 13), label, fill=colour)
        d.text((10, 10), utcnow().astimezone(timezone.utc)
               .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z", fill=(90, 105, 120))
        d.text((10, HEIGHT - 20),
               "SYNTHETIC SOURCE - replace with IP subsystem feed", fill=(120, 132, 145))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()


class OpenCVSource:
    """Real webcam, if OpenCV happens to be installed. Optional on purpose -
    a full OpenCV install on a Pi is a big commitment you do not need yet."""

    def __init__(self, index: int = 0) -> None:
        import cv2  # imported lazily so the app runs without it
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise RuntimeError("could not open camera")

    def frame(self) -> Optional[bytes]:
        ok, img = self._cap.read()
        if not ok:
            return None
        ok, enc = self._cv2.imencode(".jpg", img)
        return enc.tobytes() if ok else None


def build_source(prefer_webcam: bool = False):
    if prefer_webcam:
        try:
            return OpenCVSource()
        except Exception:
            pass  # fall through to synthetic - a blank pane is worse than a fake one
    return SyntheticSource()


def mjpeg_stream(source) -> Iterator[bytes]:
    """The multipart trick: one HTTP response that never ends, with each JPEG
    announced by its own headers. `--boundary` separates the frames."""
    interval = 1.0 / FPS
    while True:
        start = time.time()
        jpg = source.frame()
        if jpg:
            yield (b"--" + BOUNDARY.encode() + b"\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                   + jpg + b"\r\n")
        time.sleep(max(0.0, interval - (time.time() - start)))
