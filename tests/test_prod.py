"""Automated tests for gophish_api, bulk upload, and Flask UI."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

# Ensure prod/ is on sys.path when run as `python -m unittest tests.test_prod`
PROD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROD_DIR not in sys.path:
    sys.path.insert(0, PROD_DIR)


def _reload_gophish_api(**env: str) -> object:
    """Reload gophish_api with a controlled environment."""
    for key in (
        "GOPHISH_URL",
        "GOPHISH_API_KEY",
        "GOPHISH_API_TIMEOUT",
        "GOPHISH_BULK_TIMEOUT",
    ):
        os.environ.pop(key, None)
    os.environ.update(env)
    import gophish_api

    importlib.reload(gophish_api)
    return gophish_api


class TimeoutTests(unittest.TestCase):
    def test_defaults_without_env(self) -> None:
        api = _reload_gophish_api()
        self.assertEqual(api.API_TIMEOUT, 30)
        self.assertEqual(api.BULK_TIMEOUT, 600)
        self.assertEqual(api.request_timeout(), 30)
        self.assertEqual(api.request_timeout(745), 600)

    def test_env_overrides(self) -> None:
        api = _reload_gophish_api(
            GOPHISH_API_TIMEOUT="15",
            GOPHISH_BULK_TIMEOUT="900",
        )
        self.assertEqual(api.request_timeout(), 15)
        self.assertEqual(api.request_timeout(100), 900)


class ApiRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _reload_gophish_api(
            GOPHISH_URL="https://gophish.test:3333",
            GOPHISH_API_KEY="test-key",
            GOPHISH_API_TIMEOUT="30",
            GOPHISH_BULK_TIMEOUT="600",
        )

    def _mock_response(self, payload: object) -> mock.MagicMock:
        resp = mock.MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = lambda: None
        return resp

    @mock.patch("gophish_api.requests.get")
    def test_api_get_uses_short_timeout(self, mock_get: mock.MagicMock) -> None:
        mock_get.return_value = self._mock_response({})
        self.api.api_get("/campaigns/summary")
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 30)

    @mock.patch("gophish_api.requests.post")
    def test_create_group_uses_bulk_timeout(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = self._mock_response({"id": 1})
        targets = [{"email": f"u{i}@example.com"} for i in range(3)]
        self.api.create_group("BulkUpload:test", targets)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 600)

    @mock.patch("gophish_api.requests.post")
    def test_import_group_csv_uses_bulk_timeout(self, mock_post: mock.MagicMock) -> None:
        mock_post.return_value = self._mock_response([{"email": "a@b.com"}])
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("Email,First Name,Last Name\na@b.com,A,B\n")
            path = fh.name
        try:
            self.api.import_group_csv(path, expected_rows=1)
        finally:
            os.unlink(path)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 600)


class BulkUploadTests(unittest.TestCase):
    def test_count_csv_and_dry_run(self) -> None:
        import bulk_upload_userbases as bulk

        csv_path = os.path.join(PROD_DIR, "input", "userbase_01_fr_sales.csv")
        self.assertTrue(os.path.isfile(csv_path))
        count, emails = bulk.count_csv_rows_local(csv_path)
        self.assertGreater(count, 0)
        self.assertEqual(len(emails), count)

        result = bulk.process_csv(csv_path, prefix="BulkUpload:", dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.local_count, result.import_count)
        self.assertEqual(result.stored_count, result.import_count)

    def test_resolve_csv_paths_blocks_traversal(self) -> None:
        import bulk_upload_userbases as bulk

        input_dir = os.path.join(PROD_DIR, "input")
        paths = bulk.resolve_csv_paths(
            input_dir, ["userbase_01_fr_sales.csv", "../../../etc/passwd"]
        )
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("userbase_01_fr_sales.csv"))


class EnvSaveTests(unittest.TestCase):
    def test_save_credentials_preserves_bulk_timeout(self) -> None:
        import gophish_manager

        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "GOPHISH_URL=https://old.test\n"
                    "GOPHISH_API_KEY=oldkey\n"
                    "GOPHISH_BULK_TIMEOUT=600\n"
                    "GOPHISH_API_TIMEOUT=30\n"
                )
            with mock.patch.object(gophish_manager, "app") as mock_app:
                mock_app.secret_key = "test-secret"
                with mock.patch(
                    "gophish_manager.os.path.join",
                    return_value=env_path,
                ):
                    gophish_manager._save_credentials_to_env_file(
                        "https://new.test", "newkey"
                    )
            with open(env_path, encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("GOPHISH_URL=https://new.test", content)
            self.assertIn("GOPHISH_API_KEY=newkey", content)
            self.assertIn("GOPHISH_BULK_TIMEOUT=600", content)
            self.assertIn("GOPHISH_API_TIMEOUT=30", content)


class FlaskAppTests(unittest.TestCase):
    def setUp(self) -> None:
        _reload_gophish_api(
            GOPHISH_URL="https://gophish.test:3333",
            GOPHISH_API_KEY="",
        )
        import gophish_manager

        importlib.reload(gophish_manager)
        self.app = gophish_manager.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_index_without_credentials_is_fast(self) -> None:
        with mock.patch("gophish_api.api_get") as mock_get:
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 200)
            mock_get.assert_not_called()
            self.assertIn(b"Settings", resp.data)

    def test_index_with_credentials_calls_gophish(self) -> None:
        _reload_gophish_api(
            GOPHISH_URL="https://gophish.test:3333",
            GOPHISH_API_KEY="test-key",
        )
        import gophish_manager

        importlib.reload(gophish_manager)
        client = gophish_manager.app.test_client()

        summary = {"campaigns": [{"id": 1, "name": "c1"}]}
        groups = [{"id": 2, "name": "g1", "targets": [{"email": "a@b.com"}]}]
        with mock.patch("gophish_api.api_get", side_effect=[summary, groups]):
            resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"c1", resp.data)

    def test_index_survives_gophish_error(self) -> None:
        _reload_gophish_api(
            GOPHISH_URL="https://gophish.test:3333",
            GOPHISH_API_KEY="test-key",
        )
        import gophish_manager

        importlib.reload(gophish_manager)
        client = gophish_manager.app.test_client()

        with mock.patch(
            "gophish_api.api_get",
            side_effect=ConnectionError("unreachable"),
        ):
            resp = client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_ajax_upload_dry_run(self) -> None:
        _reload_gophish_api(
            GOPHISH_URL="https://gophish.test:3333",
            GOPHISH_API_KEY="test-key",
        )
        import gophish_manager

        importlib.reload(gophish_manager)
        client = gophish_manager.app.test_client()
        resp = client.post(
            "/ajax/upload-userbases",
            json={"csv_files": ["userbase_01_fr_sales.csv"], "dry_run": True},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["passed"], 1)


class BootstrapTests(unittest.TestCase):
    def test_venv_python_path_on_windows(self) -> None:
        import bootstrap

        with mock.patch("bootstrap.os.name", "nt"):
            path = bootstrap.venv_python()
        self.assertTrue(path.endswith("Scripts\\python.exe") or path.endswith("Scripts/python.exe"))


if __name__ == "__main__":
    unittest.main()
