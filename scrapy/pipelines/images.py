"""
Images Pipeline

See documentation in topics/media-pipeline.rst
"""

from __future__ import annotations

import functools
import hashlib
import math
import warnings
from collections.abc import Sequence
from contextlib import suppress
from io import BytesIO
from numbers import Real
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from itemadapter import ItemAdapter

from scrapy.exceptions import NotConfigured, ScrapyDeprecationWarning
from scrapy.http import Request, Response
from scrapy.http.request import NO_CALLBACK
from scrapy.pipelines.files import FilesPipeline, _checksum_algorithm
from scrapy.pipelines.media import FileException
from scrapy.utils.defer import ensure_awaitable
from scrapy.utils.python import get_func_args_dict

if TYPE_CHECKING:
    from collections.abc import Iterator
    from os import PathLike

    from PIL import Image

    from scrapy.crawler import Crawler
    from scrapy.pipelines.media import FileInfoOrError, MediaPipeline
    from scrapy.utils.request import RequestFingerprinterProtocol


# Request meta key holding the focal point selected for that request, as a
# (x, y) tuple of normalized coordinates.
_FOCAL_POINT_META_KEY = "image_focal_point"


def _focal_point_string(focal_point: tuple[float, float]) -> str:
    """Canonical text representation of a validated focal point.

    It is hashed into request fingerprints and stored file names, so two
    items that select the same focal point share both caches.
    """
    return f"{focal_point[0]},{focal_point[1]}"


def _focal_point_suffix(focal_point: tuple[float, float]) -> str:
    return (
        "_f"
        + hashlib.sha1(  # noqa: S324
            _focal_point_string(focal_point).encode()
        ).hexdigest()[:16]
    )


class ImageException(FileException):
    """General image error exception"""


class _FocalPointFingerprinter:
    """Fingerprinter wrapper that keeps requests with different focal points
    apart, while leaving every other request's fingerprint unchanged."""

    def __init__(self, fingerprinter: RequestFingerprinterProtocol):
        self._fingerprinter = fingerprinter

    def fingerprint(self, request: Request) -> bytes:
        fingerprint = self._fingerprinter.fingerprint(request)
        focal_point = request.meta.get(_FOCAL_POINT_META_KEY)
        if focal_point is None:
            return fingerprint
        return hashlib.sha1(  # noqa: S324
            fingerprint + b"|" + _focal_point_string(focal_point).encode()
        ).digest()


def _focal_crop_box(
    width: int,
    height: int,
    focal_point: tuple[float, float],
    target_ratio: float,
) -> tuple[int, int, int, int]:
    """Largest axis-aligned window of *target_ratio* (width / height) that
    fits into a *width* by *height* image and contains *focal_point*.

    *focal_point* coordinates are normalized (0 to 1) in the coordinate
    system of the image, so the pixel coordinates are *focal_point* times the
    image dimensions. The returned box follows the Pillow ``crop()``
    convention: (left, top, right, bottom), with the right and bottom edges
    excluded.
    """
    focal_x = focal_point[0] * width
    focal_y = focal_point[1] * height
    source_ratio = width / height
    if target_ratio >= source_ratio:
        crop_width = width
        crop_height = min(height, max(1, round(width / target_ratio)))
    else:
        crop_height = height
        crop_width = min(width, max(1, round(height * target_ratio)))
    left = round(min(max(focal_x - crop_width / 2, 0), width - crop_width))
    top = round(min(max(focal_y - crop_height / 2, 0), height - crop_height))
    return left, top, left + crop_width, top + crop_height


class ImagesPipeline(FilesPipeline):
    """Pipeline that implements the handling logic specific to images.

    Thumbnails can be cropped around per-item focal points; see
    :ref:`topics-images-focal-points`.
    """

    MEDIA_NAME: str = "image"

    # Uppercase attributes kept for backward compatibility with code that subclasses
    # ImagesPipeline. They may be overridden by settings.
    MIN_WIDTH: int = 0
    MIN_HEIGHT: int = 0
    EXPIRES: int = 90
    THUMBS: ClassVar[dict[str, tuple[int, int]]] = {}
    PRESERVE_FORMAT: bool = False
    DEFAULT_IMAGES_URLS_FIELD = "image_urls"
    DEFAULT_IMAGES_RESULT_FIELD = "images"
    DEFAULT_IMAGES_FOCAL_POINTS_FIELD = "image_focal_points"

    def __init__(
        self,
        store_uri: str | PathLike[str],
        download_func: None = None,
        *,
        crawler: Crawler,
    ):
        if download_func is not None:  # pragma: no cover
            warnings.warn(
                "The download_func argument of ImagesPipeline.__init__() is ignored"
                " and will be removed in a future Scrapy version.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )

        try:
            from PIL import Image, ImageOps  # noqa: PLC0415

            self._Image = Image
            self._ImageOps = ImageOps
        except ImportError:
            raise NotConfigured(
                "ImagesPipeline requires the Scrapy[images] extra to be installed"
            ) from None

        super().__init__(store_uri, crawler=crawler)

        settings = crawler.settings
        resolve = functools.partial(
            self._key_for_pipe,
            base_class_name="ImagesPipeline",
            settings=settings,
        )
        self.store.checksum_algorithm = _checksum_algorithm(
            settings, resolve("IMAGES_CHECKSUM_ALGORITHM")
        )
        self.expires: float = settings.getfloat(resolve("IMAGES_EXPIRES"), self.EXPIRES)

        if not hasattr(self, "IMAGES_RESULT_FIELD"):
            self.IMAGES_RESULT_FIELD: str = self.DEFAULT_IMAGES_RESULT_FIELD
        if not hasattr(self, "IMAGES_URLS_FIELD"):
            self.IMAGES_URLS_FIELD: str = self.DEFAULT_IMAGES_URLS_FIELD
        if not hasattr(self, "IMAGES_FOCAL_POINTS_FIELD"):
            self.IMAGES_FOCAL_POINTS_FIELD: str = self.DEFAULT_IMAGES_FOCAL_POINTS_FIELD

        self.images_urls_field: str = settings.get(
            resolve("IMAGES_URLS_FIELD"), self.IMAGES_URLS_FIELD
        )
        self.images_result_field: str = settings.get(
            resolve("IMAGES_RESULT_FIELD"), self.IMAGES_RESULT_FIELD
        )
        self.images_focal_points_field: str = settings.get(
            resolve("IMAGES_FOCAL_POINTS_FIELD"), self.IMAGES_FOCAL_POINTS_FIELD
        )
        # A different focal point must produce a different request identity,
        # so the per-URL result cache cannot hand back thumbnails (or skip a
        # download) computed for another item that shares the same URL.
        self._fingerprinter = _FocalPointFingerprinter(crawler.request_fingerprinter)
        self.min_width: int = settings.getint(
            resolve("IMAGES_MIN_WIDTH"), self.MIN_WIDTH
        )
        self.min_height: int = settings.getint(
            resolve("IMAGES_MIN_HEIGHT"), self.MIN_HEIGHT
        )
        self.thumbs: dict[str, tuple[int, int]] = settings.get(
            resolve("IMAGES_THUMBS"), self.THUMBS
        )
        self.preserve_format: bool = settings.getbool(
            resolve("IMAGES_PRESERVE_FORMAT"), self.PRESERVE_FORMAT
        )
        params = get_func_args_dict(self.convert_image)
        self._convert_image_takes_format: bool = "image_format" in params or any(
            param.kind is param.VAR_KEYWORD for param in params.values()
        )
        if not self._convert_image_takes_format:
            warnings.warn(
                f"{type(self).__name__}.convert_image() does not accept an"
                f" image_format keyword argument. Overriding convert_image()"
                f" without it is deprecated.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler.settings["IMAGES_STORE"], crawler=crawler)

    def _get_store_setting_name(self, name: str) -> str:
        return self._key_for_pipe(
            f"IMAGES_STORE_{name}",
            "ImagesPipeline",
            self.crawler.settings,
        )

    async def file_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        return await self.image_downloaded(response, request, info, item=item)

    async def image_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        checksum: str | None = None
        for path, image, buf in self.get_images(response, request, info, item=item):
            if checksum is None:
                buf.seek(0)
                checksum = hashlib.file_digest(
                    buf, self.store.checksum_algorithm
                ).hexdigest()
            width, height = image.size
            content_type = self._Image.MIME.get(image.format or "JPEG", "image/jpeg")
            await ensure_awaitable(
                self.store.persist_file(
                    path,
                    buf,
                    info,
                    meta={"width": width, "height": height},
                    headers={"Content-Type": content_type},
                )
            )
        assert checksum is not None
        return checksum

    def get_images(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> Iterator[tuple[str, Image.Image, BytesIO]]:
        """Yield the full-size image followed by one image per thumbnail.

        When the request carries a focal point in its
        :reqmeta:`image_focal_point` meta key, each thumbnail is the largest
        window of the thumbnail aspect ratio that contains the focal point,
        cropped from the EXIF-transposed original and then scaled. The
        full-size image and requests without a focal point follow the default
        rules; see :ref:`topics-images-focal-points`.
        """
        path = self.file_path(request, response=response, info=info, item=item)
        orig_image = self._Image.open(BytesIO(response.body))
        assert orig_image.format is not None
        image_format = orig_image.format
        transposed_image = self._ImageOps.exif_transpose(orig_image)

        width, height = transposed_image.size
        if width < self.min_width or height < self.min_height:
            raise ImageException(
                "Image too small "
                f"({width}x{height} < "
                f"{self.min_width}x{self.min_height})"
            )

        image, buf = self._convert_image(
            transposed_image,
            image_format=image_format,
            response_body=BytesIO(response.body),
        )
        yield path, image, buf

        focal_point = request.meta.get(_FOCAL_POINT_META_KEY)
        for thumb_id, size in self.thumbs.items():
            thumb_path = self.thumb_path(
                request, thumb_id, response=response, info=info, item=item
            )
            if focal_point is None:
                thumb_image, thumb_buf = self._convert_image(
                    image, size, image_format=image_format, response_body=buf
                )
            else:
                # Every thumbnail is cropped from the same EXIF-transposed
                # original pixels, never from a previously resized thumbnail.
                crop_box = _focal_crop_box(
                    width, height, focal_point, size[0] / size[1]
                )
                cropped_image = transposed_image.crop(crop_box)
                thumb_image, thumb_buf = self._convert_image(
                    cropped_image,
                    size,
                    image_format=image_format,
                    response_body=BytesIO(b""),
                )
            yield thumb_path, thumb_image, thumb_buf

    def _convert_image(
        self,
        image: Image.Image,
        size: tuple[int, int] | None = None,
        *,
        image_format: str,
        response_body: BytesIO,
    ) -> tuple[Image.Image, BytesIO]:
        if self._convert_image_takes_format:
            return self.convert_image(
                image, size, image_format=image_format, response_body=response_body
            )
        return self.convert_image(image, size, response_body=response_body)

    def convert_image(
        self,
        image: Image.Image,
        size: tuple[int, int] | None = None,
        *,
        image_format: str | None = None,
        response_body: BytesIO,
    ) -> tuple[Image.Image, BytesIO]:
        if image_format is None:
            warnings.warn(
                f"Calling {type(self).__name__}.convert_image() without an"
                f" image_format keyword argument is deprecated.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
            image_format = image.format
        if self.preserve_format and image_format is not None:
            target_format = image_format
        else:
            target_format = "JPEG"
        if target_format == "JPEG":
            if image_format in {"PNG", "WEBP"} and image.mode == "RGBA":
                background = self._Image.new("RGBA", image.size, (255, 255, 255))
                background.paste(image, image)
                image = background.convert("RGB")
            elif image.mode == "P":
                image = image.convert("RGBA")
                background = self._Image.new("RGBA", image.size, (255, 255, 255))
                background.paste(image, image)
                image = background.convert("RGB")
            elif image.mode != "RGB":
                image = image.convert("RGB")

        if size:
            image = image.copy()
            resampling_filter = self._Image.Resampling.LANCZOS
            image.thumbnail(size, resampling_filter)
        elif target_format == image_format:
            image.format = target_format
            return image, response_body

        buf = BytesIO()
        image.save(buf, target_format)
        image.format = target_format
        return image, buf

    def get_media_requests(
        self, item: Any, info: MediaPipeline.SpiderInfo
    ) -> list[Request]:
        """Build one request per image URL.

        Focal points from the *image_focal_points* item field (see
        :setting:`IMAGES_FOCAL_POINTS_FIELD`) are validated and, when present,
        attached to the matching request as its :reqmeta:`image_focal_point`
        meta key. Invalid focal points raise before any request is returned.
        """
        adapter = ItemAdapter(item)
        urls = adapter.get(self.images_urls_field, [])
        if not isinstance(urls, list):
            raise TypeError(
                f"{self.images_urls_field} must be a list of URLs, got {type(urls).__name__}. "
            )
        focal_points = self._get_item_focal_points(adapter, len(urls))
        requests = []
        for url, focal_point in zip(urls, focal_points, strict=True):
            request = Request(url, callback=NO_CALLBACK)
            if focal_point is not None:
                request.meta[_FOCAL_POINT_META_KEY] = focal_point
            requests.append(request)
        return requests

    def _get_item_focal_points(
        self, adapter: ItemAdapter, url_count: int
    ) -> list[tuple[float, float] | None]:
        """Read and validate the focal points of an item.

        The field is a list aligned with the image URLs: each entry is either
        ``None``, for the default thumbnail rule, or a pair of finite real
        numbers between 0 and 1. Everything is validated before any request is
        returned, so an invalid item fails before any file is written.
        """
        raw_points = adapter.get(self.images_focal_points_field)
        if raw_points is None:
            return [None] * url_count
        if not isinstance(raw_points, list):
            raise TypeError(
                f"{self.images_focal_points_field} must be a list aligned with"
                f" {self.images_urls_field}, got {type(raw_points).__name__}."
            )
        if len(raw_points) != url_count:
            raise ValueError(
                f"{self.images_focal_points_field} has {len(raw_points)} focal"
                f" points but {self.images_urls_field} has {url_count} URLs;"
                " the two lists must have the same length."
            )
        return [self._validate_focal_point(raw_point) for raw_point in raw_points]

    @staticmethod
    def _validate_focal_point(
        raw_point: Any,
    ) -> tuple[float, float] | None:
        if raw_point is None:
            return None
        # bool is a subclass of int, so exclude it explicitly, along with
        # strings and other non-sequence values.
        if isinstance(raw_point, bool | str | bytes) or not isinstance(
            raw_point, Sequence
        ):
            raise TypeError(
                "Each focal point must be a (x, y) pair of numbers or None,"
                f" got {raw_point!r}."
            )
        if len(raw_point) != 2:
            raise ValueError(
                f"Each focal point must hold exactly two coordinates, got"
                f" {len(raw_point)}: {list(raw_point)!r}."
            )
        coordinates: list[float] = []
        for axis, value in zip(("x", "y"), raw_point, strict=True):
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(
                    f"The {axis} coordinate of a focal point must be a real"
                    f" number, got {value!r}."
                )
            number = float(value)
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                raise ValueError(
                    f"The {axis} coordinate of a focal point must be a finite"
                    f" number between 0 and 1, got {value!r}."
                )
            coordinates.append(number)
        return cast("tuple[float, float]", tuple(coordinates))

    def item_completed(
        self, results: list[FileInfoOrError], item: Any, info: MediaPipeline.SpiderInfo
    ) -> Any:
        with suppress(KeyError):
            ItemAdapter(item)[self.images_result_field] = [x for ok, x in results if ok]
        return item

    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        image_guid = hashlib.sha1(request.url.encode()).hexdigest()  # noqa: S324
        ext = "" if self.preserve_format else ".jpg"
        focal_point = request.meta.get(_FOCAL_POINT_META_KEY)
        if focal_point is not None:
            image_guid += _focal_point_suffix(focal_point)
        return f"full/{image_guid}{ext}"

    def thumb_path(
        self,
        request: Request,
        thumb_id: str,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        thumb_guid = hashlib.sha1(request.url.encode()).hexdigest()  # noqa: S324
        ext = "" if self.preserve_format else ".jpg"
        focal_point = request.meta.get(_FOCAL_POINT_META_KEY)
        if focal_point is not None:
            thumb_guid += _focal_point_suffix(focal_point)
        return f"thumbs/{thumb_id}/{thumb_guid}{ext}"
