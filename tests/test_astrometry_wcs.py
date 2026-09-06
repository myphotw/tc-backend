from __future__ import annotations

import unittest
from unittest.mock import patch

from app.common.services.api_clients.astrometry.astrometry_client import (
    AstrometryClient,
)
from app.common.services.api_clients.astrometry.wcs_parser import (
    FITS_BLOCK_SIZE,
    WcsParseError,
    parse_wcs_fits,
)
from app.common.services.api_clients.base_client import ApiClientError


def _card(keyword: str, value: str) -> bytes:
    return f"{keyword:<8}= {value}".ljust(80).encode("ascii")


def _fits(*, use_pc: bool = False, sip: bool = True) -> bytes:
    cards = [
        _card("SIMPLE", "T"),
        _card("BITPIX", "8"),
        _card("NAXIS", "0"),
        _card("IMAGEW", "1080"),
        _card("IMAGEH", "1920"),
        _card("CTYPE1", "'RA---TAN-SIP'" if sip else "'RA---TAN'"),
        _card("CTYPE2", "'DEC--TAN-SIP'" if sip else "'DEC--TAN'"),
        _card("CUNIT1", "'deg'"),
        _card("CUNIT2", "'deg'"),
        _card("RADESYS", "'ICRS'"),
        _card("EQUINOX", "2.000D+03"),
        _card("LONPOLE", "180.0"),
        _card("LATPOLE", "0.0"),
        _card("CRVAL1", "1.069977189608935D+01"),
        _card("CRVAL2", "4.126709404265329D+01"),
        _card("CRPIX1", "540.5"),
        _card("CRPIX2", "960.5"),
    ]
    if use_pc:
        cards.extend(
            [
                _card("PC1_1", "0.0"),
                _card("PC1_2", "-1.0"),
                _card("PC2_1", "1.0"),
                _card("PC2_2", "0.0"),
                _card("CDELT1", "-2.0D-03"),
                _card("CDELT2", "2.0D-03"),
            ]
        )
    else:
        cards.extend(
            [
                _card("CD1_1", "-1.0D-03"),
                _card("CD1_2", "2.0D-03"),
                _card("CD2_1", "2.0D-03"),
                _card("CD2_2", "1.0D-03"),
            ]
        )
    if sip:
        cards.extend(
            [
                _card("A_ORDER", "2"),
                _card("B_ORDER", "2"),
                _card("AP_ORDER", "2"),
                _card("BP_ORDER", "2"),
                _card("A_0_2", "1.25D-07"),
                _card("B_2_0", "-2.5D-07"),
                _card("AP_0_2", "3.75D-07"),
                _card("BP_2_0", "-5.0D-07"),
            ]
        )
    cards.append("END".ljust(80).encode("ascii"))
    header = b"".join(cards)
    return header.ljust(
        ((len(header) + FITS_BLOCK_SIZE - 1) // FITS_BLOCK_SIZE)
        * FITS_BLOCK_SIZE,
        b" ",
    )


class _BinaryResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        content_type: str = "application/fits",
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
        }
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class _BinarySession:
    def __init__(self, *responses: _BinaryResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)

    def close(self) -> None:
        pass


class AstrometryWcsTests(unittest.TestCase):
    def test_m31_tan_sip_header_is_normalized(self) -> None:
        wcs = parse_wcs_fits(_fits())

        self.assertEqual(wcs["schema_version"], 1)
        self.assertEqual(wcs["ctype1"], "RA---TAN-SIP")
        self.assertEqual(wcs["ctype2"], "DEC--TAN-SIP")
        self.assertEqual(wcs["raster_width"], 1080)
        self.assertEqual(wcs["raster_height"], 1920)
        self.assertEqual(wcs["cunit1"], "deg")
        self.assertEqual(wcs["radesys"], "ICRS")
        self.assertEqual(wcs["equinox"], 2000.0)
        self.assertEqual(wcs["lonpole"], 180.0)
        self.assertEqual(wcs["latpole"], 0.0)
        self.assertAlmostEqual(wcs["crval1"], 10.69977189608935)
        self.assertAlmostEqual(wcs["cd11"], -0.001)
        self.assertEqual(wcs["sip"]["a_order"], 2)
        self.assertAlmostEqual(wcs["sip"]["a"]["0_2"], 1.25e-7)
        self.assertAlmostEqual(wcs["sip"]["ap"]["0_2"], 3.75e-7)
        self.assertAlmostEqual(wcs["sip"]["bp"]["2_0"], -5e-7)

    def test_pc_cdelt_is_normalized_to_cd_matrix(self) -> None:
        wcs = parse_wcs_fits(_fits(use_pc=True, sip=False))

        self.assertEqual(
            (wcs["cd11"], wcs["cd12"], wcs["cd21"], wcs["cd22"]),
            (-0.0, 0.002, 0.002, 0.0),
        )
        self.assertIsNone(wcs["sip"])

    def test_raster_falls_back_to_naxis_dimensions(self) -> None:
        artifact = _fits().replace(_card("IMAGEW", "1080"), _card("NAXIS1", "640"))
        artifact = artifact.replace(_card("IMAGEH", "1920"), _card("NAXIS2", "480"))

        wcs = parse_wcs_fits(artifact)

        self.assertEqual((wcs["raster_width"], wcs["raster_height"]), (640, 480))

    def test_malformed_missing_required_key_and_size_limit_are_rejected(self) -> None:
        with self.assertRaises(WcsParseError):
            parse_wcs_fits(b"not-fits".ljust(FITS_BLOCK_SIZE, b" "))
        with self.assertRaises(WcsParseError):
            parse_wcs_fits(_fits().replace(b"CRVAL1  =", b"IGNORED =", 1))
        with self.assertRaises(WcsParseError):
            parse_wcs_fits(_fits(), max_size=len(_fits()) - 1)

    def test_binary_fetch_retries_404_without_usage_or_json_parsing(self) -> None:
        first = _BinaryResponse(b"missing", status_code=404, content_type="text/html")
        second = _BinaryResponse(_fits())
        session = _BinarySession(first, second)
        client = AstrometryClient(api_key="test-key", session=session)
        client.retry_count = 2
        client.can_use = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("WCS fetch must not consult submission quota")
        )

        with patch(
            "app.common.services.api_clients.astrometry.astrometry_client.time.sleep"
        ):
            artifact = client.get_wcs_file(provider_job_id=16772218)

        self.assertEqual(artifact, _fits())
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(all(request["method"] == "GET" for request in session.requests))
        self.assertTrue(
            all(
                str(request["url"]).endswith("/wcs_file/16772218")
                for request in session.requests
            )
        )
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_repeated_404_and_malformed_binary_fail_closed(self) -> None:
        client = AstrometryClient(
            api_key="test-key",
            session=_BinarySession(
                _BinaryResponse(b"missing", status_code=404),
                _BinaryResponse(b"missing", status_code=404),
            ),
        )
        client.retry_count = 2
        with patch(
            "app.common.services.api_clients.astrometry.astrometry_client.time.sleep"
        ):
            with self.assertRaises(ApiClientError) as missing:
                client.get_wcs_file(provider_job_id=16772218)
        self.assertEqual(missing.exception.status_code, 404)

        malformed = AstrometryClient(
            api_key="test-key",
            session=_BinarySession(
                _BinaryResponse(
                    b"<html>not fits</html>".ljust(FITS_BLOCK_SIZE, b" "),
                    content_type="text/html",
                )
            ),
        )
        malformed.retry_count = 3
        with self.assertRaises(ApiClientError):
            malformed.get_wcs_file(provider_job_id=16772218)
        self.assertEqual(len(malformed.session.requests), 1)

    def test_binary_fetch_enforces_response_size_limit(self) -> None:
        session = _BinarySession(_BinaryResponse(_fits()))
        client = AstrometryClient(api_key="test-key", session=session)

        with self.assertRaises(ApiClientError):
            client.get_wcs_file(provider_job_id=16772218, max_bytes=1024)

        self.assertEqual(len(session.requests), 1)
        self.assertTrue(session.responses == [])


if __name__ == "__main__":
    unittest.main()
