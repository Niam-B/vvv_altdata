"""fetch_acs_income: parsing of both sources and loud failure."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.fetch_acs_income as acs  # noqa: E402


class Resp:
    def __init__(self, text, status=200, ctype="application/json"):
        self.text, self.status_code, self.content = text, status, text.encode()
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestACS(unittest.TestCase):
    def test_api_parse(self):
        body = '[["B19013_001E","zip code tabulation area"],["55000","01001"],["-666666666","01002"]]'
        with mock.patch.object(acs.requests, "get", return_value=Resp(body)):
            self.assertEqual(acs.from_api(2023), {"01001": 55000})

    def test_api_non_json_raises(self):
        with mock.patch.object(acs.requests, "get", return_value=Resp("<html>error</html>", ctype="text/html")):
            with self.assertRaisesRegex(RuntimeError, "non-JSON"):
                acs.from_api(2023)

    def test_summary_file_parse(self):
        body = ("GEO_ID|B19013_E001|B19013_M001\n0400000US01|62000|300\n"
                "860Z200US01001|71000|5000\n860Z200US01002|-666666666|-222222222\n")
        with mock.patch.object(acs.requests, "get", return_value=Resp(body, ctype="text/plain")):
            self.assertEqual(acs.from_summary_file(2023), {"01001": 71000})

    def test_fails_loudly(self):
        out = Path(tempfile.mkdtemp()) / "zcta_income.csv"
        with mock.patch.object(acs, "OUT", out), \
                mock.patch.object(acs.requests, "get", return_value=Resp("", status=503)), \
                mock.patch.object(sys, "argv", ["x", "--force"]):
            self.assertEqual(acs.main(), 1)
        self.assertFalse(out.exists())

    def test_too_few_rows_rejected(self):
        out = Path(tempfile.mkdtemp()) / "zcta_income.csv"
        body = '[["B19013_001E","zip code tabulation area"],["55000","01001"]]'
        with mock.patch.object(acs, "OUT", out), \
                mock.patch.object(acs.requests, "get", return_value=Resp(body)), \
                mock.patch.object(sys, "argv", ["x", "--force"]):
            self.assertEqual(acs.main(), 1)


if __name__ == "__main__":
    unittest.main()
