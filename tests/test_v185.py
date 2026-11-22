from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from PIL import Image

import engine


CORE = engine.get_mod()


def image_bytes(size, encoding):
    stream = BytesIO()
    Image.new("RGB", size, "blue").save(stream, encoding)
    return stream.getvalue()


class HighestQualityAuditTests(unittest.TestCase):
    def test_live_tries_second_hd_entry_when_primary_returns_normal_playback(self):
        main = "https://www.douyin.com/aweme/v1/play/?video_id=video12345678&ratio=default&watermark=0"
        low = "https://v1-web.douyinvod.com/adaptive"
        choices = [{"urls": [main], "inspect_first": True},
                   {"urls": [low], "width": 720, "height": 960, "bitrate": 900000}]
        hosts = []

        def download(urls, path, _referer):
            hostname = urlsplit(urls[0]).hostname
            hosts.append(hostname)
            path.write_bytes(b"high" if hostname == "www.iesdouyin.com" else b"low")

        def probe(path):
            width, height, bitrate = (1308, 1744, 7500000) if path.read_bytes() == b"high" else (720, 960, 900000)
            return {"width": width, "height": height, "bitrate": bitrate, "bytes": path.stat().st_size}

        with (tempfile.TemporaryDirectory() as directory,
              patch.object(CORE, "_download_douyin_video", side_effect=download),
              patch.object(CORE, "probe_video_file", side_effect=probe)):
            target = Path(directory) / "live.mp4"
            quality = CORE.download_douyin_live_motion(choices, target, "", allow_lower=False, reference_size=(1308, 1744))
        self.assertEqual(hosts, ["www.douyin.com", "www.iesdouyin.com"])
        self.assertEqual((quality["width"], quality["height"]), (1308, 1744))
        self.assertEqual(quality["live_playback_quality"], "hd_playback_mirror")

    def test_cdn_image_continuation_honors_byte_range(self):
        requested = []
        class Response(BytesIO):
            status = 206
            headers = {"Content-Length": "4", "Content-Range": "bytes 4-7/8", "ETag": '"original"'}

        class Opener:
            def open(self, request, timeout):
                requested.append(dict(request.header_items()))
                return Response(b"EFGH")

        data = CORE.complete_image_response(Opener(), "https://ci.xiaohongshu.com/media", {}, b"ABCD",
                                            "bytes 0-3/8", '"original"', 16)
        self.assertEqual(data, b"ABCDEFGH")
        self.assertEqual(requested[0]["Range"], "bytes=4-7")
        self.assertEqual(requested[0]["If-range"], '"original"')

    def test_rednote_tries_all_raw_hosts_before_same_size_render(self):
        raw_one = "https://ci.xiaohongshu.com/file"
        raw_two = "https://sns-img-bd.xhscdn.com/file"
        rendered = raw_one + "?imageView2/2/w/0/format/jpg/v3"
        latest = image_bytes((40, 60), "PNG")
        tried = []

        def fetch(url, **kwargs):
            tried.append((url, kwargs["complete_image"]))
            if url == raw_one:
                raise CORE.ArchiveError("fetch_failed", "timeout", 69)
            return latest, "image/png", url

        with patch.object(CORE, "fetch_limited", side_effect=fetch):
            obtained, fallback, size = CORE.fetch_xhs_image([raw_one, rendered, raw_two], [], (40, 60), "")
        self.assertEqual(tried, [(raw_one, True), (raw_two, True)])
        self.assertFalse(fallback)
        self.assertEqual((obtained[0], size), (latest, (40, 60)))

    def test_same_size_jpeg_render_preserves_archived_png(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, temporary, rendered = root / "photo.png", root / "candidate.jpg", root / "photo.jpg"
            original.write_bytes(image_bytes((40, 60), "PNG"))
            temporary.write_bytes(image_bytes((40, 60), "JPEG"))
            path, size, retained = CORE.commit_image(temporary, rendered, (40, 60))
            self.assertEqual((path, size, retained), (original, (40, 60), True))
            self.assertFalse(rendered.exists())
