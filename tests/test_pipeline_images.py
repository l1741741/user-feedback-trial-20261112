from __future__ import annotations

import dataclasses
import hashlib
import io
import math
import random
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import Any
from unittest import mock

import attr
import pytest
from itemadapter import ItemAdapter

from scrapy.exceptions import NotConfigured, ScrapyDeprecationWarning
from scrapy.http import Request, Response
from scrapy.item import Field, Item
from scrapy.pipelines.files import FilesPipeline, GCSFilesStore, S3FilesStore
from scrapy.pipelines.images import (
    ImageException,
    ImagesPipeline,
    _focal_crop_box,
    _focal_point_suffix,
)
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.test_pipeline_files import CrawlerAwareStore
from tests.utils.cloud import mock_google_cloud_storage
from tests.utils.decorators import coroutine_test
from tests.utils.media_pipelines import DUMMY_SPIDER_INFO

try:
    from PIL import Image
except ImportError:
    pytest.skip(
        "Missing Python Imaging Library, install https://pypi.org/pypi/Pillow",
        allow_module_level=True,
    )
else:
    encoders = {"jpeg_encoder", "jpeg_decoder"}
    if not encoders.issubset(set(Image.core.__dict__)):  # type: ignore[attr-defined]
        pytest.skip("Missing JPEG encoders", allow_module_level=True)


class TestImagesPipeline:
    def setup_method(self):
        self.tempdir = mkdtemp()
        crawler = get_crawler(None, {"IMAGES_STORE": self.tempdir})
        self.pipeline = build_from_crawler(ImagesPipeline, crawler)

    def teardown_method(self):
        rmtree(self.tempdir)

    def test_missing_pillow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "PIL", None)
        with pytest.raises(NotConfigured, match=r"requires the Scrapy\[images\] extra"):
            ImagesPipeline(self.tempdir, crawler=get_crawler())

    def test_file_path(self):
        file_path = self.pipeline.file_path
        assert (
            file_path(Request("https://dev.mydeco.com/mydeco.gif"))
            == "full/3fd165099d8e71b8a48b2683946e64dbfad8b52d.jpg"
        )
        assert (
            file_path(
                Request(
                    "http://www.maddiebrown.co.uk///catalogue-items//image_54642_12175_95307.jpg"
                )
            )
            == "full/0ffcd85d563bca45e2f90becd0ca737bc58a00b2.jpg"
        )
        assert (
            file_path(
                Request("https://dev.mydeco.com/two/dirs/with%20spaces%2Bsigns.gif")
            )
            == "full/b250e3a74fff2e4703e310048a5b13eba79379d2.jpg"
        )
        assert (
            file_path(
                Request(
                    "http://www.dfsonline.co.uk/get_prod_image.php?img=status_0907_mdm.jpg"
                )
            )
            == "full/4507be485f38b0da8a0be9eb2e1dfab8a19223f2.jpg"
        )
        assert (
            file_path(Request("http://www.dorma.co.uk/images/product_details/2532/"))
            == "full/97ee6f8a46cbbb418ea91502fd24176865cf39b2.jpg"
        )
        assert (
            file_path(Request("http://www.dorma.co.uk/images/product_details/2532"))
            == "full/244e0dd7d96a3b7b01f54eded250c9e272577aa1.jpg"
        )
        assert (
            file_path(
                Request("http://www.dorma.co.uk/images/product_details/2532"),
                response=Response("http://www.dorma.co.uk/images/product_details/2532"),
                info=DUMMY_SPIDER_INFO,
            )
            == "full/244e0dd7d96a3b7b01f54eded250c9e272577aa1.jpg"
        )

    def test_file_path_preserve_format(self):
        pipeline = build_from_crawler(
            ImagesPipeline,
            get_crawler(
                None, {"IMAGES_STORE": self.tempdir, "IMAGES_PRESERVE_FORMAT": True}
            ),
        )
        assert (
            pipeline.file_path(Request("https://dev.mydeco.com/mydeco.gif"))
            == "full/3fd165099d8e71b8a48b2683946e64dbfad8b52d"
        )
        assert (
            pipeline.thumb_path(Request("https://dev.mydeco.com/mydeco.gif"), "50")
            == "thumbs/50/3fd165099d8e71b8a48b2683946e64dbfad8b52d"
        )

    def test_thumbnail_name(self):
        thumb_path = self.pipeline.thumb_path
        name = "50"
        assert (
            thumb_path(Request("file:///tmp/foo.jpg"), name)
            == "thumbs/50/38a86208c36e59d4404db9e37ce04be863ef0335.jpg"
        )
        assert (
            thumb_path(Request("file://foo.png"), name)
            == "thumbs/50/3f7f26a0132d8c3df6b56f0691b89083df00d62a.jpg"
        )
        assert (
            thumb_path(Request("file:///tmp/foo"), name)
            == "thumbs/50/0329ad83ebb8e93ea7c7906d46e9ed55f7349a50.jpg"
        )
        assert (
            thumb_path(Request("file:///tmp/some.name/foo"), name)
            == "thumbs/50/850233df65a5b83361798f532f1fc549cd13cbe9.jpg"
        )
        assert (
            thumb_path(
                Request("file:///tmp/some.name/foo"),
                name,
                response=Response("file:///tmp/some.name/foo"),
                info=DUMMY_SPIDER_INFO,
            )
            == "thumbs/50/850233df65a5b83361798f532f1fc549cd13cbe9.jpg"
        )

    def test_thumbnail_name_from_item(self):
        """
        Custom thumbnail name based on item data, overriding default implementation
        """

        class CustomImagesPipeline(ImagesPipeline):
            def thumb_path(
                self, request, thumb_id, response=None, info=None, item=None
            ) -> str:
                return f"thumb/{thumb_id}/{item.get('path')}"

        thumb_path = build_from_crawler(
            CustomImagesPipeline, get_crawler(None, {"IMAGES_STORE": self.tempdir})
        ).thumb_path
        item = {"path": "path-to-store-file"}
        request = Request("http://example.com")
        assert (
            thumb_path(request, "small", item=item) == "thumb/small/path-to-store-file"
        )

    def test_get_images_exception(self):
        self.pipeline.min_width = 100
        self.pipeline.min_height = 100

        _, buf1 = _create_image("JPEG", "RGB", (50, 50), (0, 0, 0))
        _, buf2 = _create_image("JPEG", "RGB", (150, 50), (0, 0, 0))
        _, buf3 = _create_image("JPEG", "RGB", (50, 150), (0, 0, 0))

        resp1 = Response(url="https://dev.mydeco.com/mydeco.gif", body=buf1.getvalue())
        resp2 = Response(url="https://dev.mydeco.com/mydeco.gif", body=buf2.getvalue())
        resp3 = Response(url="https://dev.mydeco.com/mydeco.gif", body=buf3.getvalue())
        req = Request(url="https://dev.mydeco.com/mydeco.gif")

        with pytest.raises(ImageException):
            next(
                self.pipeline.get_images(
                    response=resp1, request=req, info=DUMMY_SPIDER_INFO
                )
            )
        with pytest.raises(ImageException):
            next(
                self.pipeline.get_images(
                    response=resp2, request=req, info=DUMMY_SPIDER_INFO
                )
            )
        with pytest.raises(ImageException):
            next(
                self.pipeline.get_images(
                    response=resp3, request=req, info=DUMMY_SPIDER_INFO
                )
            )

    def test_get_images(self):
        self.pipeline.min_width = 0
        self.pipeline.min_height = 0
        self.pipeline.thumbs = {"small": (20, 20)}

        orig_im, buf = _create_image("JPEG", "RGB", (50, 50), (0, 0, 0))
        _, orig_thumb_buf = _create_image("JPEG", "RGB", (20, 20), (0, 0, 0))
        resp = Response(url="https://dev.mydeco.com/mydeco.gif", body=buf.getvalue())
        req = Request(url="https://dev.mydeco.com/mydeco.gif")

        get_images_gen = self.pipeline.get_images(
            response=resp, request=req, info=DUMMY_SPIDER_INFO
        )

        path, new_im, new_buf = next(get_images_gen)
        assert path == "full/3fd165099d8e71b8a48b2683946e64dbfad8b52d.jpg"
        assert orig_im.copy() == new_im
        assert buf.getvalue() == new_buf.getvalue()

        thumb_path, _, thumb_buf = next(get_images_gen)
        assert thumb_path == "thumbs/small/3fd165099d8e71b8a48b2683946e64dbfad8b52d.jpg"
        assert orig_thumb_buf.getvalue() == thumb_buf.getvalue()

    def test_get_images_convert_image_without_image_format(self):
        class OldImagesPipeline(ImagesPipeline):
            def convert_image(self, image, size=None, *, response_body):  # type: ignore[override]
                return super().convert_image(image, size, response_body=response_body)

        crawler = get_crawler(None, {"IMAGES_STORE": self.tempdir})
        with pytest.warns(ScrapyDeprecationWarning, match="Overriding convert_image"):
            pipeline = build_from_crawler(OldImagesPipeline, crawler)
        _, buf = _create_image("JPEG", "RGB", (50, 50), (0, 0, 0))
        resp = Response(url="https://example.com/image.jpg", body=buf.getvalue())
        req = Request(url="https://example.com/image.jpg")
        with pytest.warns(ScrapyDeprecationWarning, match="without an image_format"):
            _, image, _ = next(
                pipeline.get_images(response=resp, request=req, info=DUMMY_SPIDER_INFO)
            )
        assert image.format == "JPEG"

    def test_get_transposed_images(self):
        orig_im = Image.new("RGB", (2, 2), (0, 0, 0))
        orig_im.putpixel((1, 1), (255, 0, 0))
        exif = orig_im.getexif()
        exif[274] = 3
        buf = io.BytesIO()
        orig_im.save(buf, "PNG", exif=exif)
        buf.seek(0)

        resp = Response(url="https://dev.mydeco.com/mydeco.gif", body=buf.getvalue())
        req = Request(url="https://dev.mydeco.com/mydeco.gif")

        get_images_gen = self.pipeline.get_images(
            response=resp, request=req, info=DUMMY_SPIDER_INFO
        )

        path, new_im, _ = next(get_images_gen)
        assert path == "full/3fd165099d8e71b8a48b2683946e64dbfad8b52d.jpg"
        assert new_im.getpixel((0, 0)) == (255, 0, 0)

    @coroutine_test
    async def test_image_downloaded(self) -> None:
        """The image and its thumbnails are stored, and the checksum of the
        full-size image is returned."""
        self.pipeline.thumbs = {"small": (20, 20)}
        _, buf = _create_image("JPEG", "RGB", (50, 50), (0, 0, 0))
        url = "https://dev.mydeco.com/mydeco.gif"
        response = Response(url=url, body=buf.getvalue())

        checksum = await self.pipeline.image_downloaded(
            response, Request(url=url), DUMMY_SPIDER_INFO
        )

        buf.seek(0)
        assert checksum == hashlib.file_digest(buf, "md5").hexdigest()
        name = "3fd165099d8e71b8a48b2683946e64dbfad8b52d.jpg"
        assert Path(self.tempdir, "full", name).read_bytes() == buf.getvalue()
        assert Path(self.tempdir, "thumbs", "small", name).exists()

    @coroutine_test
    async def test_image_downloaded_checksum_algorithm(self) -> None:
        crawler = get_crawler(settings_dict={"IMAGES_CHECKSUM_ALGORITHM": "sha256"})
        pipeline = ImagesPipeline(self.tempdir, crawler=crawler)
        _, buf = _create_image("JPEG", "RGB", (50, 50), (0, 0, 0))
        url = "https://dev.mydeco.com/mydeco.gif"
        response = Response(url=url, body=buf.getvalue())

        checksum = await pipeline.image_downloaded(
            response, Request(url=url), DUMMY_SPIDER_INFO
        )

        assert checksum == hashlib.sha256(buf.getvalue()).hexdigest()

    def test_convert_image_preserve_format(self):
        pipeline = build_from_crawler(
            ImagesPipeline,
            get_crawler(
                None, {"IMAGES_STORE": self.tempdir, "IMAGES_PRESERVE_FORMAT": True}
            ),
        )

        SIZE = (100, 100)
        COLOUR = (0, 127, 255, 50)
        im, buf = _create_image("PNG", "RGBA", SIZE, COLOUR)

        converted, converted_buf = pipeline.convert_image(
            im, image_format="PNG", response_body=buf
        )
        assert converted.mode == "RGBA"
        assert converted.format == "PNG"
        # the original bytes are reused, no re-encoding happens
        assert converted_buf == buf

        thumbnail, thumbnail_buf = pipeline.convert_image(
            converted,
            size=(10, 25),
            image_format="PNG",
            response_body=converted_buf,
        )
        assert thumbnail.mode == "RGBA"
        assert thumbnail.format == "PNG"
        assert thumbnail.size == (10, 10)
        assert Image.open(thumbnail_buf).format == "PNG"

    @coroutine_test
    async def test_image_downloaded_preserve_format(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = build_from_crawler(
            ImagesPipeline,
            get_crawler(
                None, {"IMAGES_STORE": self.tempdir, "IMAGES_PRESERVE_FORMAT": True}
            ),
        )
        pipeline.thumbs = {"small": (20, 20)}
        _, buf = _create_image("PNG", "RGBA", (50, 50), (0, 127, 255, 50))
        url = "https://dev.mydeco.com/mydeco.gif"
        response = Response(url=url, body=buf.getvalue())

        persisted = []
        monkeypatch.setattr(
            pipeline.store,
            "persist_file",
            lambda path, b, info, meta=None, headers=None: persisted.append(
                (path, b.getvalue(), headers)
            ),
        )

        await pipeline.image_downloaded(response, Request(url=url), DUMMY_SPIDER_INFO)

        assert len(persisted) == 2
        full_path, full_bytes, full_headers = persisted[0]
        assert full_path == "full/3fd165099d8e71b8a48b2683946e64dbfad8b52d"
        assert full_bytes == buf.getvalue()
        assert full_headers == {"Content-Type": "image/png"}

        _, thumb_bytes, thumb_headers = persisted[1]
        assert Image.open(io.BytesIO(thumb_bytes)).format == "PNG"
        assert thumb_headers == {"Content-Type": "image/png"}

    def test_convert_image(self):
        SIZE = (100, 100)
        # straight forward case: RGB and JPEG
        COLOUR: tuple[int, ...] = (0, 127, 255)
        im, buf = _create_image("JPEG", "RGB", SIZE, COLOUR)
        converted, converted_buf = self.pipeline.convert_image(
            im, image_format="JPEG", response_body=buf
        )
        assert converted.mode == "RGB"
        assert converted.getcolors() == [(10000, COLOUR)]
        # check that we don't convert JPEGs again
        assert converted_buf == buf

        # check that thumbnail keep image ratio
        thumbnail, _ = self.pipeline.convert_image(
            converted, size=(10, 25), image_format="JPEG", response_body=converted_buf
        )
        assert thumbnail.mode == "RGB"
        assert thumbnail.size == (10, 10)

        # transparency case: RGBA and PNG
        COLOUR = (0, 127, 255, 50)
        im, buf = _create_image("PNG", "RGBA", SIZE, COLOUR)
        converted, _ = self.pipeline.convert_image(
            im, image_format="PNG", response_body=buf
        )
        assert converted.mode == "RGB"
        assert converted.getcolors() == [(10000, (205, 230, 255))]

        # transparency case with palette: P and PNG
        COLOUR = (0, 127, 255, 50)
        im, buf = _create_image("PNG", "RGBA", SIZE, COLOUR)
        im = im.convert("P")
        converted, _ = self.pipeline.convert_image(
            im, image_format="PNG", response_body=buf
        )
        assert converted.mode == "RGB"
        assert converted.getcolors() == [(10000, (205, 230, 255))]

        # grayscale case: L and PNG
        im, buf = _create_image("PNG", "L", SIZE, 128)
        converted, _ = self.pipeline.convert_image(
            im, image_format="PNG", response_body=buf
        )
        assert converted.mode == "RGB"
        assert converted.getcolors() == [(10000, (128, 128, 128))]

    def test_convert_image_without_image_format(self):
        im, buf = _create_image("PNG", "RGBA", (100, 100), (0, 127, 255, 50))
        with pytest.warns(ScrapyDeprecationWarning, match="without an image_format"):
            converted, _ = self.pipeline.convert_image(im, response_body=buf)
        # The format of the image is used, so transparency becomes white.
        assert converted.mode == "RGB"
        assert converted.getcolors() == [(10000, (205, 230, 255))]

    @pytest.mark.parametrize(
        "bad_type",
        [
            "http://example.com/file.jpg",
            ("http://example.com/file.jpg",),
            {"url": "http://example.com/file.jpg"},
            123,
            None,
        ],
    )
    def test_rejects_non_list_image_urls(self, tmp_path, bad_type):
        pipeline = build_from_crawler(
            ImagesPipeline, get_crawler(None, {"IMAGES_STORE": str(tmp_path)})
        )
        item = ImagesPipelineTestItem()
        item["image_urls"] = bad_type

        with pytest.raises(TypeError, match="image_urls must be a list of URLs"):
            list(pipeline.get_media_requests(item, DUMMY_SPIDER_INFO))


class TestImagesPipelineFieldsMixin(ABC):
    @property
    @abstractmethod
    def item_class(self) -> Any:
        raise NotImplementedError

    def test_item_fields_default(self):
        url = "http://www.example.com/images/1.jpg"
        item = self.item_class(name="item1", image_urls=[url])
        pipeline = build_from_crawler(
            ImagesPipeline, get_crawler(None, {"IMAGES_STORE": "s3://example/images/"})
        )
        requests = list(pipeline.get_media_requests(item, DUMMY_SPIDER_INFO))
        assert requests[0].url == url
        results: Any = [(True, {"url": url})]
        item = pipeline.item_completed(results, item, DUMMY_SPIDER_INFO)
        images = ItemAdapter(item).get("images")
        assert images == [results[0][1]]
        assert isinstance(item, self.item_class)

    def test_item_fields_override_settings(self):
        url = "http://www.example.com/images/1.jpg"
        item = self.item_class(name="item1", custom_image_urls=[url])
        pipeline = build_from_crawler(
            ImagesPipeline,
            get_crawler(
                None,
                {
                    "IMAGES_STORE": "s3://example/images/",
                    "IMAGES_URLS_FIELD": "custom_image_urls",
                    "IMAGES_RESULT_FIELD": "custom_images",
                },
            ),
        )
        requests = list(pipeline.get_media_requests(item, DUMMY_SPIDER_INFO))
        assert requests[0].url == url
        results: Any = [(True, {"url": url})]
        item = pipeline.item_completed(results, item, DUMMY_SPIDER_INFO)
        custom_images = ItemAdapter(item).get("custom_images")
        assert custom_images == [results[0][1]]
        assert isinstance(item, self.item_class)


class TestImagesPipelineFieldsDict(TestImagesPipelineFieldsMixin):
    item_class = dict


class ImagesPipelineTestItem(Item):
    name = Field()
    # default fields
    image_urls = Field()
    images = Field()
    # overridden fields
    custom_image_urls = Field()
    custom_images = Field()


class TestImagesPipelineFieldsItem(TestImagesPipelineFieldsMixin):
    item_class = ImagesPipelineTestItem


@dataclasses.dataclass
class ImagesPipelineTestDataClass:
    name: str
    # default fields
    image_urls: list[str] = dataclasses.field(default_factory=list)
    images: list[dict[str, str]] = dataclasses.field(default_factory=list)
    # overridden fields
    custom_image_urls: list[str] = dataclasses.field(default_factory=list)
    custom_images: list[dict[str, str]] = dataclasses.field(default_factory=list)


class TestImagesPipelineFieldsDataClass(TestImagesPipelineFieldsMixin):
    item_class = ImagesPipelineTestDataClass


@attr.s
class ImagesPipelineTestAttrsItem:
    name = attr.ib(default="")
    # default fields
    image_urls: list[str] = attr.ib(factory=list)
    images: list[dict[str, str]] = attr.ib(factory=list)
    # overridden fields
    custom_image_urls: list[str] = attr.ib(factory=list)
    custom_images: list[dict[str, str]] = attr.ib(factory=list)


class TestImagesPipelineFieldsAttrsItem(TestImagesPipelineFieldsMixin):
    item_class = ImagesPipelineTestAttrsItem


class TestImagesPipelineCustomSettings:
    img_cls_attribute_names = [
        # Pipeline attribute names with corresponding setting names.
        ("EXPIRES", "IMAGES_EXPIRES"),
        ("MIN_WIDTH", "IMAGES_MIN_WIDTH"),
        ("MIN_HEIGHT", "IMAGES_MIN_HEIGHT"),
        ("IMAGES_URLS_FIELD", "IMAGES_URLS_FIELD"),
        ("IMAGES_RESULT_FIELD", "IMAGES_RESULT_FIELD"),
        ("THUMBS", "IMAGES_THUMBS"),
    ]

    # This should match what is defined in ImagesPipeline.
    default_pipeline_settings = {
        "MIN_WIDTH": 0,
        "MIN_HEIGHT": 0,
        "EXPIRES": 90,
        "THUMBS": {},
        "IMAGES_URLS_FIELD": "image_urls",
        "IMAGES_RESULT_FIELD": "images",
    }

    def _generate_fake_settings(
        self, tmp_path: Path, prefix: str | None = None
    ) -> dict[str, Any]:
        """
        :param prefix: string for setting keys
        :return: dictionary of image pipeline settings
        """

        def random_string() -> str:
            return "".join([chr(random.randint(97, 123)) for _ in range(10)])

        settings = {
            "IMAGES_EXPIRES": random.randint(100, 1000),
            "IMAGES_STORE": tmp_path,
            "IMAGES_RESULT_FIELD": random_string(),
            "IMAGES_URLS_FIELD": random_string(),
            "IMAGES_MIN_WIDTH": random.randint(1, 1000),
            "IMAGES_MIN_HEIGHT": random.randint(1, 1000),
            "IMAGES_THUMBS": {
                "small": (random.randint(1, 1000), random.randint(1, 1000)),
                "big": (random.randint(1, 1000), random.randint(1, 1000)),
            },
        }
        if not prefix:
            return settings

        return {
            prefix.upper() + "_" + k if k != "IMAGES_STORE" else k: v
            for k, v in settings.items()
        }

    def _generate_fake_pipeline_subclass(self) -> type[ImagesPipeline]:
        """
        :return: ImagePipeline class will all uppercase attributes set.
        """

        class UserDefinedImagePipeline(ImagesPipeline):
            # Values should be in different range than fake_settings.
            MIN_WIDTH = random.randint(1000, 2000)
            MIN_HEIGHT = random.randint(1000, 2000)
            THUMBS = {
                "small": (random.randint(1000, 2000), random.randint(1000, 2000)),
                "big": (random.randint(1000, 2000), random.randint(1000, 2000)),
            }
            EXPIRES = random.randint(1000, 2000)
            IMAGES_URLS_FIELD = "field_one"
            IMAGES_RESULT_FIELD = "field_two"

        return UserDefinedImagePipeline

    def test_different_settings_for_different_instances(self, tmp_path):
        """
        If there are two instances of ImagesPipeline class with different settings, they should
        have different settings.
        """
        custom_settings = self._generate_fake_settings(tmp_path)
        default_sts_pipe = build_from_crawler(
            ImagesPipeline, get_crawler(None, {"IMAGES_STORE": tmp_path})
        )
        user_sts_pipe = build_from_crawler(
            ImagesPipeline, get_crawler(None, custom_settings)
        )
        for pipe_attr, settings_attr in self.img_cls_attribute_names:
            expected_default_value = self.default_pipeline_settings.get(pipe_attr)
            custom_value = custom_settings.get(settings_attr)
            assert expected_default_value != custom_value
            assert (
                getattr(default_sts_pipe, pipe_attr.lower()) == expected_default_value
            )
            assert getattr(user_sts_pipe, pipe_attr.lower()) == custom_value

    def test_subclass_attrs_preserved_default_settings(self, tmp_path):
        """
        If image settings are not defined at all subclass of ImagePipeline takes values
        from class attributes.
        """
        pipeline_cls = self._generate_fake_pipeline_subclass()
        pipeline = build_from_crawler(
            pipeline_cls, get_crawler(None, {"IMAGES_STORE": tmp_path})
        )
        for pipe_attr, _ in self.img_cls_attribute_names:
            # Instance attribute (lowercase) must be equal to class attribute (uppercase).
            attr_value = getattr(pipeline, pipe_attr.lower())
            assert attr_value != self.default_pipeline_settings[pipe_attr]
            assert attr_value == getattr(pipeline, pipe_attr)

    def test_subclass_attrs_preserved_custom_settings(self, tmp_path):
        """
        If image settings are defined but they are not defined for subclass default
        values taken from settings should be preserved.
        """
        pipeline_cls = self._generate_fake_pipeline_subclass()
        settings = self._generate_fake_settings(tmp_path)
        pipeline = build_from_crawler(pipeline_cls, get_crawler(None, settings))
        for pipe_attr, settings_attr in self.img_cls_attribute_names:
            # Instance attribute (lowercase) must be equal to
            # value defined in settings.
            value = getattr(pipeline, pipe_attr.lower())
            assert value != self.default_pipeline_settings[pipe_attr]
            setings_value = settings.get(settings_attr)
            assert value == setings_value

    def test_no_custom_settings_for_subclasses(self, tmp_path):
        """
        If there are no settings for subclass and no subclass attributes, pipeline should use
        attributes of base class.
        """

        class UserDefinedImagePipeline(ImagesPipeline):
            pass

        user_pipeline = build_from_crawler(
            UserDefinedImagePipeline, get_crawler(None, {"IMAGES_STORE": tmp_path})
        )
        for pipe_attr, _ in self.img_cls_attribute_names:
            # Values from settings for custom pipeline should be set on pipeline instance.
            custom_value = self.default_pipeline_settings.get(pipe_attr.upper())
            assert getattr(user_pipeline, pipe_attr.lower()) == custom_value

    def test_custom_settings_for_subclasses(self, tmp_path):
        """
        If there are custom settings for subclass and NO class attributes, pipeline should use custom
        settings.
        """

        class UserDefinedImagePipeline(ImagesPipeline):
            pass

        prefix = UserDefinedImagePipeline.__name__.upper()
        settings = self._generate_fake_settings(tmp_path, prefix=prefix)
        user_pipeline = build_from_crawler(
            UserDefinedImagePipeline, get_crawler(None, settings)
        )
        for pipe_attr, settings_attr in self.img_cls_attribute_names:
            # Values from settings for custom pipeline should be set on pipeline instance.
            custom_value = settings.get(prefix + "_" + settings_attr)
            assert custom_value != self.default_pipeline_settings[pipe_attr]
            assert getattr(user_pipeline, pipe_attr.lower()) == custom_value

    def test_custom_settings_and_class_attrs_for_subclasses(self, tmp_path):
        """
        If there are custom settings for subclass AND class attributes
        setting keys are preferred and override attributes.
        """
        pipeline_cls = self._generate_fake_pipeline_subclass()
        prefix = pipeline_cls.__name__.upper()
        settings = self._generate_fake_settings(tmp_path, prefix=prefix)
        user_pipeline = build_from_crawler(pipeline_cls, get_crawler(None, settings))
        for pipe_attr, settings_attr in self.img_cls_attribute_names:
            custom_value = settings.get(prefix + "_" + settings_attr)
            assert custom_value != self.default_pipeline_settings[pipe_attr]
            assert getattr(user_pipeline, pipe_attr.lower()) == custom_value

    def test_cls_attrs_with_DEFAULT_prefix(self, tmp_path):
        class UserDefinedImagePipeline(ImagesPipeline):
            DEFAULT_IMAGES_URLS_FIELD = "something"
            DEFAULT_IMAGES_RESULT_FIELD = "something_else"

        pipeline = build_from_crawler(
            UserDefinedImagePipeline, get_crawler(None, {"IMAGES_STORE": tmp_path})
        )
        assert (
            pipeline.images_result_field
            == UserDefinedImagePipeline.DEFAULT_IMAGES_RESULT_FIELD
        )
        assert (
            pipeline.images_urls_field
            == UserDefinedImagePipeline.DEFAULT_IMAGES_URLS_FIELD
        )

    def test_user_defined_subclass_default_key_names(self, tmp_path):
        """Test situation when user defines subclass of ImagePipeline,
        but uses attribute names for default pipeline (without prefixing
        them with pipeline class name).
        """
        settings = self._generate_fake_settings(tmp_path)

        class UserPipe(ImagesPipeline):
            pass

        pipeline_cls = build_from_crawler(UserPipe, get_crawler(None, settings))

        for pipe_attr, settings_attr in self.img_cls_attribute_names:
            expected_value = settings.get(settings_attr)
            assert getattr(pipeline_cls, pipe_attr.lower()) == expected_value

    def test_media_storages_setting_name(self):
        crawler = get_crawler(
            None,
            {
                "IMAGES_STORE": "mystore://example.com/",
                "MEDIA_STORAGES": {
                    "mystore": "tests.test_pipeline_files.CrawlerAwareStore"
                },
            },
        )
        store = build_from_crawler(ImagesPipeline, crawler).store
        assert isinstance(store, CrawlerAwareStore)
        assert store.acl_setting == "IMAGES_STORE_S3_ACL"

    @pytest.mark.requires_botocore
    def test_images_store_s3_acl_setting_used(self):
        crawler = get_crawler(
            None,
            {
                "FILES_STORE": "s3://bucket/files/",
                "IMAGES_STORE": "s3://bucket/images/",
                "FILES_STORE_S3_ACL": "private",
                "IMAGES_STORE_S3_ACL": "public-read",
            },
        )

        files_store = build_from_crawler(FilesPipeline, crawler).store
        images_store = build_from_crawler(ImagesPipeline, crawler).store
        assert isinstance(files_store, S3FilesStore)
        assert isinstance(images_store, S3FilesStore)
        assert files_store.POLICY == "private"
        assert images_store.POLICY == "public-read"
        assert S3FilesStore.POLICY == "private"

    def test_images_store_gcs_acl_setting_used(self):
        pytest.importorskip("google.cloud.storage")

        client_mock, bucket_mock, _ = mock_google_cloud_storage()
        bucket_mock.test_iam_permissions.return_value = [
            "storage.objects.get",
            "storage.objects.create",
        ]
        crawler = get_crawler(
            None,
            {
                "FILES_STORE": "gs://bucket/files/",
                "IMAGES_STORE": "gs://bucket/images/",
                "FILES_STORE_GCS_ACL": "",
                "IMAGES_STORE_GCS_ACL": "authenticatedRead",
            },
        )

        with mock.patch("google.cloud.storage.Client", return_value=client_mock):
            files_store = build_from_crawler(FilesPipeline, crawler).store
            images_store = build_from_crawler(ImagesPipeline, crawler).store
        assert isinstance(files_store, GCSFilesStore)
        assert isinstance(images_store, GCSFilesStore)
        assert files_store.POLICY is None
        assert images_store.POLICY == "authenticatedRead"
        assert GCSFilesStore.POLICY is None


def _create_image(format_: str, *a: Any, **kw: Any) -> tuple[Image.Image, io.BytesIO]:
    buf = io.BytesIO()
    Image.new(*a, **kw).save(buf, format_)
    buf.seek(0)
    return Image.open(buf), buf


def _image_bytes(image: Image.Image, format_: str = "PNG", **kw: Any) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format_, **kw)
    return buf.getvalue()


def _color_grid(width: int, height: int, columns: int, rows: int) -> Image.Image:
    """Solid-color cells laid out over a grid, each cell filled with one
    distinct color. Cell centers can be located on a resized copy."""
    image = Image.new("RGB", (width, height))
    cell_width = width // columns
    cell_height = height // rows
    for row in range(rows):
        for column in range(columns):
            color = (
                (column * 255) // max(columns - 1, 1),
                (row * 255) // max(rows - 1, 1),
                128,
            )
            image.paste(
                color,
                (
                    column * cell_width,
                    row * cell_height,
                    (column + 1) * cell_width if column < columns - 1 else width,
                    (row + 1) * cell_height if row < rows - 1 else height,
                ),
            )
    return image


class TestFocalCropBox:
    @pytest.mark.parametrize(
        ("size", "focal_point", "target_ratio", "expected"),
        [
            # square window on a square image: the whole image
            ((100, 100), (0.5, 0.5), 1.0, (0, 0, 100, 100)),
            # corner focal points clamp the window to the edges
            ((100, 100), (0.0, 0.0), 1.0, (0, 0, 100, 100)),
            ((100, 100), (1.0, 1.0), 1.0, (0, 0, 100, 100)),
            # wide window on a square image keeps the full width
            ((100, 100), (0.5, 0.5), 2.0, (0, 25, 100, 75)),
            # focal point on the left edge clamps the window left
            ((100, 100), (0.0, 0.5), 2.0, (0, 25, 100, 75)),
            # focal point on the right edge clamps the window right
            ((100, 100), (1.0, 0.5), 2.0, (0, 25, 100, 75)),
            # tall window keeps the full height, centered horizontally
            ((100, 100), (0.5, 0.5), 0.5, (25, 0, 75, 100)),
            # off-center focal point shifts the window until an edge hits
            ((100, 100), (0.1, 0.5), 0.5, (0, 0, 50, 100)),
            ((100, 100), (0.9, 0.5), 0.5, (50, 0, 100, 100)),
            # ultra-wide source: the window is width-limited
            ((100, 10), (0.5, 0.5), 1.0, (45, 0, 55, 10)),
            # ultra-tall source: the window is height-limited
            ((10, 100), (0.5, 0.5), 1.0, (0, 45, 10, 55)),
        ],
    )
    def test_crop_box(
        self,
        size: tuple[int, int],
        focal_point: tuple[float, float],
        target_ratio: float,
        expected: tuple[int, int, int, int],
    ) -> None:
        assert _focal_crop_box(*size, focal_point, target_ratio) == expected

    @pytest.mark.parametrize(
        "focal_point",
        [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.37, 0.83)],
    )
    def test_window_contains_focal_point(self, focal_point: tuple[float, float]):
        width, height = 120, 80
        left, top, right, bottom = _focal_crop_box(width, height, focal_point, 16 / 9)
        assert 0 <= left < right <= width
        assert 0 <= top < bottom <= height
        assert left <= focal_point[0] * width <= right
        assert top <= focal_point[1] * height <= bottom


class TestFocalCropPipeline:
    def setup_method(self):
        self.tempdir = mkdtemp()
        crawler = get_crawler(
            None,
            {
                "IMAGES_STORE": self.tempdir,
                "IMAGES_THUMBS": {"square": (40, 40), "wide": (80, 20)},
            },
        )
        self.pipeline = build_from_crawler(ImagesPipeline, crawler)
        self.url = "https://example.com/image.png"
        self.request = Request(self.url)

    def teardown_method(self):
        rmtree(self.tempdir)

    def _response(self, image: Image.Image) -> Response:
        return Response(url=self.url, body=_image_bytes(image))

    def _focal_request(self, focal_point: tuple[float, float]) -> Request:
        return Request(
            self.url,
            meta={"image_focal_point": focal_point},
        )

    @staticmethod
    def _rgb_pixel(
        image: Image.Image, position: tuple[int, int]
    ) -> tuple[int, int, int]:
        """Typed view of an RGB/RGBA pixel for value comparisons."""
        pixel = image.getpixel(position)
        assert isinstance(pixel, tuple)
        red, green, blue = pixel[0], pixel[1], pixel[2]
        assert isinstance(red, int)
        assert isinstance(green, int)
        assert isinstance(blue, int)
        return red, green, blue

    def test_requests_carry_validated_focal_points(self) -> None:
        item = {
            "image_urls": [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
            "image_focal_points": [None, (0.25, 0.75), (1.0, 0.0)],
        }
        requests = self.pipeline.get_media_requests(item, DUMMY_SPIDER_INFO)
        assert requests[0].meta == {}
        assert requests[1].meta == {"image_focal_point": (0.25, 0.75)}
        assert requests[2].meta == {"image_focal_point": (1.0, 0.0)}

    def test_missing_focal_points_field_keeps_default_rule(self) -> None:
        item = {"image_urls": [self.url, self.url]}
        requests = self.pipeline.get_media_requests(item, DUMMY_SPIDER_INFO)
        assert all(request.meta == {} for request in requests)

    @pytest.mark.parametrize(
        ("focal_points", "match"),
        [
            (True, "must be a list"),
            ((0.5, 0.5), "must be a list"),
            ("(0.5, 0.5)", "must be a list"),
            ([[0.5, 0.5], [0.5, 0.5]], "same length"),
            ([[0.5, 0.5, 0.5]], "exactly two coordinates"),
            ([[]], "exactly two coordinates"),
            ([(0.5,)], "exactly two coordinates"),
            ([(0.5, 0.5, 0.5)], "exactly two coordinates"),
            ([True], "pair of numbers or None"),
            ([False], "pair of numbers or None"),
            (["x"], "pair of numbers or None"),
            ([b"x"], "pair of numbers or None"),
            ([42], "pair of numbers or None"),
            ([(True, 0.5)], "real number"),
            ([(0.5, False)], "real number"),
            ([("0.5", 0.5)], "real number"),
            ([(None, 0.5)], "real number"),
            ([(math.nan, 0.5)], "finite"),
            ([(0.5, math.inf)], "finite"),
            ([(-math.inf, 0.5)], "finite"),
            ([(-0.1, 0.5)], "between 0 and 1"),
            ([(0.5, 1.1)], "between 0 and 1"),
        ],
    )
    def test_invalid_focal_points_fail_before_requests(
        self, focal_points: Any, match: str
    ) -> None:
        item = {
            "image_urls": [self.url],
            "image_focal_points": focal_points,
        }
        with pytest.raises((TypeError, ValueError), match=match):
            self.pipeline.get_media_requests(item, DUMMY_SPIDER_INFO)

    def test_boundary_coordinates_are_accepted(self) -> None:
        item = {
            "image_urls": [self.url, self.url],
            "image_focal_points": [(0.0, 0.0), (1.0, 1.0)],
        }
        requests = self.pipeline.get_media_requests(item, DUMMY_SPIDER_INFO)
        assert requests[0].meta["image_focal_point"] == (0.0, 0.0)
        assert requests[1].meta["image_focal_point"] == (1.0, 1.0)

    def test_focal_points_field_override_on_subclass(self) -> None:
        class _CustomFieldPipeline(ImagesPipeline):
            IMAGES_FOCAL_POINTS_FIELD = "focus"

        pipeline = build_from_crawler(
            _CustomFieldPipeline,
            get_crawler(None, {"IMAGES_STORE": self.tempdir}),
        )
        assert pipeline.images_focal_points_field == "focus"
        item = {
            "image_urls": [self.url],
            "focus": [(0.5, 0.5)],
        }
        requests = pipeline.get_media_requests(item, DUMMY_SPIDER_INFO)
        assert requests[0].meta["image_focal_point"] == (0.5, 0.5)

    def test_focal_points_field_override_via_setting(self) -> None:
        class _CustomFieldPipeline(ImagesPipeline):
            pass

        pipeline = build_from_crawler(
            _CustomFieldPipeline,
            get_crawler(
                None,
                {
                    "IMAGES_STORE": self.tempdir,
                    "_CUSTOMFIELDPIPELINE_IMAGES_FOCAL_POINTS_FIELD": "focus",
                },
            ),
        )
        assert pipeline.images_focal_points_field == "focus"

    def test_invalid_focal_points_write_nothing(self) -> None:
        item = {
            "image_urls": [self.url],
            "image_focal_points": [(2.0, 0.5)],
        }
        with pytest.raises(ValueError, match="between 0 and 1"):
            self.pipeline.get_media_requests(item, DUMMY_SPIDER_INFO)
        assert not list(Path(self.tempdir).rglob("*"))

    @staticmethod
    def _two_color_image() -> Image.Image:
        # red left half, blue right half: each half is a solid 40x40 block
        image = Image.new("RGB", (80, 40), (255, 0, 0))
        image.paste(Image.new("RGB", (40, 40), (0, 0, 255)), (40, 0))
        return image

    def test_focal_thumbnail_crop_rectangle_and_pixels(self) -> None:
        image = self._two_color_image()
        request = self._focal_request((0.25, 0.5))  # center of the red half
        results = list(
            self.pipeline.get_images(self._response(image), request, DUMMY_SPIDER_INFO)
        )
        paths = [path for path, _, _ in results]
        # 1:1 window around the red half is exactly the left 40x40 block
        assert paths[1] == (
            "thumbs/square/"
            + hashlib.sha1(self.url.encode()).hexdigest()
            + _focal_point_suffix((0.25, 0.5))
            + ".jpg"
        )
        square = results[1][1]
        assert square.size == (40, 40)
        # the window only contains red pixels, sampled away from the edge
        assert self._rgb_pixel(square, (5, 5)) == (255, 0, 0)
        assert self._rgb_pixel(square, (35, 35)) == (255, 0, 0)
        # the wide thumbnail keeps the full 2:1 frame, red then blue
        wide = results[2][1]
        assert wide.size == (80, 20)
        left_pixel = self._rgb_pixel(wide, (5, 10))
        right_pixel = self._rgb_pixel(wide, (75, 10))
        assert left_pixel[0] > left_pixel[2]
        assert right_pixel[2] > right_pixel[0]

    def test_different_focal_points_yield_different_windows(self) -> None:
        image = self._two_color_image()
        left_request = self._focal_request((0.0, 0.5))
        right_request = self._focal_request((1.0, 0.5))
        left_thumb = list(
            self.pipeline.get_images(
                self._response(image), left_request, DUMMY_SPIDER_INFO
            )
        )[1][1]
        right_thumb = list(
            self.pipeline.get_images(
                self._response(image), right_request, DUMMY_SPIDER_INFO
            )
        )[1][1]
        assert left_thumb.tobytes() != right_thumb.tobytes()
        # corner focal points clamp the square window onto each half
        assert left_thumb.getpixel((5, 5)) == (255, 0, 0)
        assert right_thumb.getpixel((5, 5)) == (0, 0, 255)

    def test_ultra_wide_and_ultra_tall_images(self) -> None:
        wide = _color_grid(100, 10, 5, 1)
        tall = _color_grid(10, 100, 1, 5)
        for source in (wide, tall):
            results = list(
                self.pipeline.get_images(
                    self._response(source),
                    self._focal_request((0.5, 0.5)),
                    DUMMY_SPIDER_INFO,
                )
            )
            _, thumb, _ = results[1]
            # the 1:1 window is limited by the short dimension, never padded
            assert thumb.size == (10, 10)
            # all requested thumbnail names are still produced
            assert {path.split("/")[1] for path, _, _ in results[1:]} == {
                "square",
                "wide",
            }

    def test_small_image_is_not_upscaled_or_padded(self) -> None:
        image = _color_grid(8, 8, 2, 2)
        _, thumb, _ = list(
            self.pipeline.get_images(
                self._response(image),
                self._focal_request((0.5, 0.5)),
                DUMMY_SPIDER_INFO,
            )
        )[1]
        # thumbnail() never enlarges and no padding is ever added
        assert thumb.size == (8, 8)

    def test_transparent_edges_are_flattened_for_jpeg(self) -> None:
        image = Image.new("RGBA", (40, 40), (255, 0, 0, 0))
        opaque = Image.new("RGBA", (20, 20), (0, 127, 255, 255))
        image.paste(opaque, (10, 10), opaque)
        results = list(
            self.pipeline.get_images(
                self._response(image),
                self._focal_request((0.5, 0.5)),
                DUMMY_SPIDER_INFO,
            )
        )
        _, thumb, thumb_buf = results[1]
        assert thumb.mode == "RGB"
        decoded = Image.open(thumb_buf)
        # fully transparent pixels are flattened onto a white background
        corner = self._rgb_pixel(decoded, (0, 0))
        assert corner[0] >= 230
        assert corner[1] >= 230
        assert corner[2] >= 230
        center = self._rgb_pixel(decoded, (20, 20))
        assert center[2] > center[0]

    def test_transparency_preserved_with_preserve_format(self) -> None:
        pipeline = build_from_crawler(
            ImagesPipeline,
            get_crawler(
                None,
                {
                    "IMAGES_STORE": self.tempdir,
                    "IMAGES_PRESERVE_FORMAT": True,
                    "IMAGES_THUMBS": {"square": (20, 20)},
                },
            ),
        )
        image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        response = Response(url=self.url, body=_image_bytes(image))
        _, thumb, thumb_buf = list(
            pipeline.get_images(
                response,
                self._focal_request((0.5, 0.5)),
                DUMMY_SPIDER_INFO,
            )
        )[1]
        assert thumb.mode == "RGBA"
        alpha_pixel = Image.open(thumb_buf).getpixel((0, 0))
        assert isinstance(alpha_pixel, tuple)
        assert alpha_pixel[3] == 0

    def test_exif_orientation_uses_transposed_coordinates(self) -> None:
        # a 2x1 image whose red pixel is at (1, 0) in stored orientation
        image = Image.new("RGB", (2, 1), (0, 0, 0))
        image.putpixel((1, 0), (255, 0, 0))
        exif = image.getexif()
        exif[274] = 3  # 180 degree rotation
        response = Response(url=self.url, body=_image_bytes(image, exif=exif))
        # after transposition the red pixel is at (0, 0); focal (0, 0)
        # must select the window containing it
        _, thumb, _ = list(
            self.pipeline.get_images(
                response,
                self._focal_request((0.0, 0.0)),
                DUMMY_SPIDER_INFO,
            )
        )[1]
        assert self._rgb_pixel(thumb, (0, 0))[0] == 255

    def test_multiple_thumbnails_share_the_same_original_pixels(self) -> None:
        image = self._two_color_image()
        results = list(
            self.pipeline.get_images(
                self._response(image),
                self._focal_request((0.25, 0.5)),
                DUMMY_SPIDER_INFO,
            )
        )
        assert [path.split("/")[1] for path, _, _ in results[1:]] == [
            "square",
            "wide",
        ]
        square, wide = results[1][1], results[2][1]
        assert square.size == (40, 40)
        assert wide.size == (80, 20)
        # both crops keep the red landmark that surrounds the focal point
        assert self._rgb_pixel(square, (5, 5)) == (255, 0, 0)
        wide_pixel = self._rgb_pixel(wide, (5, 10))
        assert wide_pixel[0] > wide_pixel[2]

    def test_full_image_and_response_are_not_modified(self) -> None:
        image = _color_grid(80, 40, 4, 2)
        body = _image_bytes(image)
        response = Response(url=self.url, body=body)
        results = list(
            self.pipeline.get_images(
                response,
                self._focal_request((0.5, 0.5)),
                DUMMY_SPIDER_INFO,
            )
        )
        # response body untouched
        assert response.body == body
        # the full-size output keeps the original dimensions
        full = results[0][1]
        assert full.size == (80, 40)

    def test_default_thumbnail_rule_unchanged_without_focal_point(self) -> None:
        image = self._two_color_image()
        body = _image_bytes(image)
        response = Response(url=self.url, body=body)
        default_results = list(
            self.pipeline.get_images(response, self.request, DUMMY_SPIDER_INFO)
        )
        guid = hashlib.sha1(self.url.encode()).hexdigest()
        assert default_results[0][0] == f"full/{guid}.jpg"
        assert default_results[1][0] == f"thumbs/square/{guid}.jpg"
        # a plain pipeline without the focal feature gives identical bytes
        plain_crawler = get_crawler(
            None,
            {
                "IMAGES_STORE": self.tempdir,
                "IMAGES_THUMBS": {"square": (40, 40), "wide": (80, 20)},
            },
        )
        plain = build_from_crawler(ImagesPipeline, plain_crawler)
        plain_results = list(
            plain.get_images(
                Response(url=self.url, body=body),
                Request(self.url),
                DUMMY_SPIDER_INFO,
            )
        )
        for (_, _, focal_buf), (_, _, plain_buf) in zip(
            default_results, plain_results, strict=True
        ):
            assert focal_buf.getvalue() == plain_buf.getvalue()

    def test_focal_paths_differ_from_default_paths(self) -> None:
        focal_point = (0.25, 0.75)
        suffix = _focal_point_suffix(focal_point)
        guid = hashlib.sha1(self.url.encode()).hexdigest()
        request = self._focal_request(focal_point)
        assert self.pipeline.file_path(request) == f"full/{guid}{suffix}.jpg"
        assert self.pipeline.thumb_path(request, "square") == (
            f"thumbs/square/{guid}{suffix}.jpg"
        )
        # distinct focal points get distinct names
        other = self._focal_request((0.75, 0.25))
        assert self.pipeline.file_path(other) != self.pipeline.file_path(request)
        # equal focal points get equal names
        assert self.pipeline.file_path(self._focal_request(focal_point)) == (
            self.pipeline.file_path(request)
        )

    def test_fingerprints_distinguish_focal_points(self) -> None:
        base = Request(self.url)
        left = self._focal_request((0.0, 0.5))
        right = self._focal_request((1.0, 0.5))
        fingerprinter = self.pipeline._fingerprinter
        # requests without a focal point keep the stock fingerprint
        assert fingerprinter.fingerprint(base) == (
            self.pipeline.crawler.request_fingerprinter.fingerprint(base)
        )
        assert fingerprinter.fingerprint(left) != fingerprinter.fingerprint(base)
        assert fingerprinter.fingerprint(left) != fingerprinter.fingerprint(right)
        assert fingerprinter.fingerprint(self._focal_request((0.0, 0.5))) == (
            fingerprinter.fingerprint(left)
        )

    @coroutine_test
    async def test_repeated_export_is_deterministic(self) -> None:
        image = _color_grid(80, 40, 4, 2)
        request = self._focal_request((0.25, 0.5))
        first = {
            path: buf.getvalue()
            for path, _, buf in self.pipeline.get_images(
                self._response(image), request, DUMMY_SPIDER_INFO
            )
        }
        second = {
            path: buf.getvalue()
            for path, _, buf in self.pipeline.get_images(
                self._response(image), request, DUMMY_SPIDER_INFO
            )
        }
        assert first == second

    @coroutine_test
    async def test_cross_item_same_url_uses_separate_cache_and_files(
        self,
    ) -> None:
        body = _image_bytes(self._two_color_image())

        class _LocalResponseImagesPipeline(ImagesPipeline):
            """Serve the in-memory image instead of performing a network
            request, while keeping the full pipeline machinery."""

            async def _check_media_to_download(  # type: ignore[override]
                self, request, info, *, item=None
            ):
                response = Response(url=request.url, body=body)
                return await self.media_downloaded(response, request, info, item=item)

        crawler = get_crawler(
            DefaultSpider,
            {
                "IMAGES_STORE": self.tempdir,
                "IMAGES_THUMBS": {"square": (40, 40)},
            },
        )
        crawler.spider = crawler._create_spider()
        pipeline = build_from_crawler(_LocalResponseImagesPipeline, crawler)
        pipeline.open_spider()

        left_item: dict[str, Any] = {
            "image_urls": [self.url],
            "image_focal_points": [(0.0, 0.5)],
        }
        right_item: dict[str, Any] = {
            "image_urls": [self.url],
            "image_focal_points": [(1.0, 0.5)],
        }
        for item in (left_item, right_item):
            await pipeline.process_item(item)

        info = pipeline.spiderinfo
        # both focal variants were processed, not deduplicated onto one cache
        fingerprints = list(info.downloaded)
        assert len(fingerprints) == 2
        assert len(set(fingerprints)) == 2

        results: list[dict[str, Any]] = left_item["images"] + right_item["images"]
        paths = {entry["path"] for entry in results}
        assert len(paths) == 2
        stored_thumbs = list(Path(self.tempdir, "thumbs", "square").iterdir())
        assert len(stored_thumbs) == 2
        stored_full = list(Path(self.tempdir, "full").iterdir())
        assert len(stored_full) == 2
        for entry in results:
            assert Path(self.tempdir, entry["path"]).exists()
            assert entry["status"] == "downloaded"
            assert entry["url"] == self.url

        # the two stored thumbnails hold different pixels
        thumb_bytes = {
            path.read_bytes()
            for path in Path(self.tempdir, "thumbs", "square").iterdir()
        }
        assert len(thumb_bytes) == 2

        # a third item without a focal point takes the legacy path, never a
        # focal-cropped cache entry
        plain_item: dict[str, Any] = {"image_urls": [self.url]}
        await pipeline.process_item(plain_item)
        plain_path = plain_item["images"][0]["path"]
        guid = hashlib.sha1(self.url.encode()).hexdigest()
        assert plain_path == f"full/{guid}.jpg"
        assert Path(self.tempdir, plain_path).exists()
        assert len(list(info.downloaded)) == 3
