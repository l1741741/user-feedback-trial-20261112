"""Pixel-level helpers for verifying stroke rendering."""
import io

import cairocffi as cairo


def load(png_bytes_or_path):
    if isinstance(png_bytes_or_path, (bytes, bytearray)):
        img = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes_or_path))
    else:
        img = cairo.ImageSurface.create_from_png(png_bytes_or_path)
    buf = memoryview(img.get_data()).cast('B')
    stride = img.get_stride()
    return img, buf, stride, img.get_width(), img.get_height()


def pixel(buf, stride, x, y):
    """Return (r, g, b, a) for a pixel."""
    i = y * stride + x * 4
    return buf[i + 2], buf[i + 1], buf[i], buf[i + 3]


def red_mask(img, buf, stride, w, h):
    return [
        [buf[y * stride + x * 4 + 2] > 150
         and buf[y * stride + x * 4 + 1] < 100
         and buf[y * stride + x * 4] < 100
         and buf[y * stride + x * 4 + 3] > 0
         for x in range(w)]
        for y in range(h)]


def components(mask):
    """Count 4-connected True components in a 2D boolean mask."""
    h = len(mask)
    w = len(mask[0])
    seen = [[False] * w for _ in range(h)]
    count = 0
    for y in range(h):
        for x in range(w):
            if mask[y][x] and not seen[y][x]:
                count += 1
                stack = [(x, y)]
                seen[y][x] = True
                while stack:
                    cx, cy = stack.pop()
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                                   (cx, cy + 1), (cx, cy - 1)):
                        if (0 <= nx < w and 0 <= ny < h
                                and mask[ny][nx] and not seen[ny][nx]):
                            seen[ny][nx] = True
                            stack.append((nx, ny))
    return count


def ink_bbox(buf, stride, w, h, predicate=None):
    """Return (minx, miny, maxx, maxy) of pixels passing predicate (default: alpha)."""
    if predicate is None:
        def predicate(r, g, b, a):
            return a > 0
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixel(buf, stride, x, y)
            if predicate(r, g, b, a):
                minx = min(minx, x)
                miny = min(miny, y)
                maxx = max(maxx, x)
                maxy = max(maxy, y)
    if maxx < 0:
        return None
    return minx, miny, maxx, maxy
