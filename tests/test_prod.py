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
        import gophish_bulk_config as bulk_cfg

        importlib.reload(bulk_cfg)
        self.assertEqual(bulk_cfg.API_TIMEOUT, 30)
        self.assertEqual(bulk_cfg.BULK_TIMEOUT, 1200)
        self.assertEqual(api.request_timeout(), 30)
        self.assertEqual(api.request_timeout(745), min(1200 + int(745 * 0.75), 3600))

    def test_env_overrides(self) -> None:
        api = _reload_gophish_api(
            GOPHISH_API_TIMEOUT="15",
            GOPHISH_BULK_TIMEOUT="900",
            GOPHISH_BULK_TIMEOUT_MAX="1000",
        )
        self.assertEqual(api.request_timeout(), 15)
        self.assertEqual(api.request_timeout(100), 975)


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
        self.assertEqual(
            mock_post.call_args.kwargs["timeout"],
            self.api.request_timeout(len(targets)),
        )

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
    def test_group_name_matches_csv_stem(self) -> None:
        import bulk_upload_userbases as bulk

        path = os.path.join(PROD_DIR, "input", "userbase_04_es_finance.csv")
        self.assertEqual(bulk.group_name_for_csv(path), "userbase_04_es_finance")

    def test_count_csv_and_dry_run(self) -> None:
        import bulk_upload_userbases as bulk

        csv_path = os.path.join(PROD_DIR, "input", "userbase_01_fr_sales.csv")
        self.assertTrue(os.path.isfile(csv_path))
        count, emails = bulk.count_csv_rows_local(csv_path)
        self.assertGreater(count, 0)
        self.assertEqual(len(emails), count)

        result = bulk.process_csv(csv_path, dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.group_name, "userbase_01_fr_sales")
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


class GophishUrlTests(unittest.TestCase):
    def test_normalize_adds_https(self) -> None:
        api = _reload_gophish_api()
        self.assertEqual(
            api.normalize_gophish_url("gophish.example.com:3333"),
            "https://gophish.example.com:3333",
        )
        self.assertEqual(
            api.normalize_gophish_url("https://127.0.0.1:3333/"),
            "https://127.0.0.1:3333",
        )

    def test_validate_local_and_remote(self) -> None:
        api = _reload_gophish_api()
        self.assertTrue(api.validate_gophish_url("https://127.0.0.1:3333")[0])
        self.assertTrue(api.validate_gophish_url("https://ab.example.com:3333")[0])
        self.assertFalse(api.validate_gophish_url("not-a-url")[0])


class FormatApiErrorTests(unittest.TestCase):
    def test_localhost_refused_is_actionable(self) -> None:
        api = _reload_gophish_api(
            GOPHISH_URL="https://127.0.0.1:3333",
            GOPHISH_API_KEY="key",
        )
        import requests

        err = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='127.0.0.1', port=3333): "
            "Failed to establish a new connection: [WinError 10061]"
        )
        msg = api.format_api_error(err)
        self.assertIn("127.0.0.1:3333", msg)
        self.assertIn("Settings", msg)


class BulkCreateCampaignTests(unittest.TestCase):
    def test_default_campaign_name(self) -> None:
        from bulk_create_campaigns import default_campaign_name
        from datetime import datetime, timezone

        when = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            default_campaign_name("AFR DocuSign_English", when),
            "PROD AFR DocuSign_English 2026-05-26",
        )

    # ------------------------------------------------------------------
    # Groups summary endpoint + fallback
    # ------------------------------------------------------------------
    @mock.patch("gophish_api.api_get")
    def test_fetch_groups_uses_summary_endpoint(self, mock_get: mock.MagicMock) -> None:
        """_fetch_groups_safe should prefer /groups/summary."""
        from bulk_create_campaigns import _fetch_groups_safe

        mock_get.return_value = {
            "total": 2,
            "groups": [
                {"id": 1, "name": "G1", "num_targets": 5, "modified_date": ""},
                {"id": 2, "name": "G2", "num_targets": 10, "modified_date": ""},
            ],
        }
        groups = _fetch_groups_safe()
        self.assertEqual(len(groups), 2)
        self.assertEqual(mock_get.call_args.args[0], "/groups/summary")

    @mock.patch("gophish_api.api_get")
    def test_fetch_groups_falls_back_on_error(self, mock_get: mock.MagicMock) -> None:
        """Falls back to /groups/ when summary raises."""
        from bulk_create_campaigns import _fetch_groups_safe

        full = [{"id": 1, "name": "G1", "targets": [{"email": "a@b.com"}]}]
        mock_get.side_effect = [Exception("no summary"), full]
        groups = _fetch_groups_safe()
        self.assertEqual(len(groups), 1)
        calls = [c.args[0] for c in mock_get.call_args_list]
        self.assertIn("/groups/", calls)

    @mock.patch("gophish_api.api_get")
    def test_summarize_groups_reads_num_targets(self, _mock_get: mock.MagicMock) -> None:
        """summarize_groups should use num_targets from summary response."""
        from bulk_create_campaigns import summarize_groups

        rows = summarize_groups([
            {"id": 1, "name": "G1", "num_targets": 7},
            {"id": 2, "name": "G2", "targets": [{"email": "a@b.com"}, {"email": "b@b.com"}]},
        ])
        self.assertEqual(rows[0]["num_users"], 7)
        self.assertEqual(rows[1]["num_users"], 2)

    # ------------------------------------------------------------------
    # Parallel fetch
    # ------------------------------------------------------------------
    @mock.patch("gophish_api.api_get")
    def test_fetch_create_resources_parallel(self, mock_get: mock.MagicMock) -> None:
        """fetch_create_resources returns all four keys in correct shape."""
        from bulk_create_campaigns import fetch_create_resources

        def _side(path):
            if path == "/groups/summary":
                return {"total": 1, "groups": [{"id": 1, "name": "G", "num_targets": 3}]}
            if path == "/templates/":
                return [{"id": 1, "name": "T1"}]
            if path == "/pages/":
                return [{"id": 1, "name": "P1"}]
            if path == "/smtp/":
                return [{"id": 1, "name": "S1"}]
            return []

        mock_get.side_effect = _side
        data = fetch_create_resources()
        self.assertIn("groups", data)
        self.assertIn("templates", data)
        self.assertIn("pages", data)
        self.assertIn("smtp_profiles", data)
        self.assertEqual(data["groups"][0]["num_users"], 3)
        self.assertEqual(data["templates"][0]["name"], "T1")

    # ------------------------------------------------------------------
    # Duplicate-name guard
    # ------------------------------------------------------------------
    @mock.patch("gophish_bulk_config.cooldown_final")
    @mock.patch("gophish_api.cooldown")
    @mock.patch("gophish_api.api_get")
    @mock.patch("gophish_api.api_post")
    def test_existing_campaign_name_blocked(
        self, mock_post: mock.MagicMock, mock_get: mock.MagicMock,
        _mock_cd: mock.MagicMock, _mock_cdf: mock.MagicMock,
    ) -> None:
        """A row whose campaign_name already exists should fail with an error."""
        from bulk_create_campaigns import run_bulk_create

        mock_get.return_value = {
            "campaigns": [{"name": "PROD Test Group 2026-05-26"}]
        }
        results, code = run_bulk_create(
            [
                {
                    "group_name": "Test Group",
                    "campaign_name": "PROD Test Group 2026-05-26",
                    "template_name": "T1",
                    "page_name": "P1",
                    "smtp_name": "S1",
                }
            ],
            phishing_url="http://localhost",
            recheck=False,
        )
        self.assertEqual(code, 1)
        self.assertFalse(results[0].ok)
        self.assertIn("already exists", results[0].errors[0])
        mock_post.assert_not_called()

    @mock.patch("gophish_bulk_config.cooldown_final")
    @mock.patch("gophish_api.cooldown")
    @mock.patch("gophish_api.api_get")
    @mock.patch("gophish_api.api_post")
    def test_in_batch_duplicate_name_blocked(
        self, mock_post: mock.MagicMock, mock_get: mock.MagicMock,
        _mock_cd: mock.MagicMock, _mock_cdf: mock.MagicMock,
    ) -> None:
        """Two rows with the same campaign name — second should fail."""
        from bulk_create_campaigns import run_bulk_create

        mock_get.return_value = {"campaigns": []}
        mock_post.return_value = {"id": 1}
        items = [
            {"group_name": "G1", "campaign_name": "Same Name", "template_name": "T", "page_name": "P", "smtp_name": "S"},
            {"group_name": "G2", "campaign_name": "Same Name", "template_name": "T", "page_name": "P", "smtp_name": "S"},
        ]
        results, code = run_bulk_create(items, phishing_url="http://localhost", recheck=False)
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertIn("Duplicate", results[1].errors[0])

    # ------------------------------------------------------------------
    # Recheck safety: skip already-created campaigns
    # ------------------------------------------------------------------
    @mock.patch("gophish_bulk_config.cooldown_final")
    @mock.patch("gophish_api.cooldown")
    @mock.patch("gophish_api.api_get")
    @mock.patch("gophish_api.api_post")
    def test_recheck_skips_existing_campaign(
        self,
        mock_post: mock.MagicMock,
        mock_get: mock.MagicMock,
        mock_cooldown: mock.MagicMock,
        mock_cooldown_final: mock.MagicMock,
    ) -> None:
        """Recheck should not POST a campaign whose name already exists in Gophish."""
        import bulk_create_campaigns as bcc

        # Pre-populate existing_names so the first attempt is blocked by duplicate guard.
        original_fetch = bcc._fetch_existing_campaign_names
        bcc._fetch_existing_campaign_names = lambda: {"prod test 2026-06-17"}
        try:
            results, code = bcc.run_bulk_create(
                [
                    {
                        "group_name": "G",
                        "campaign_name": "PROD Test 2026-06-17",
                        "template_name": "T",
                        "page_name": "P",
                        "smtp_name": "S",
                    }
                ],
                phishing_url="http://localhost",
                recheck=True,
            )
        finally:
            bcc._fetch_existing_campaign_names = original_fetch

        # The name was already in Gophish: recheck should mark the row ok
        # (it was already created, so no POST should be made).
        self.assertTrue(results[0].ok)
        mock_post.assert_not_called()

    # ------------------------------------------------------------------
    # Original tests preserved
    # ------------------------------------------------------------------
    @mock.patch("gophish_bulk_config.cooldown_final")
    @mock.patch("gophish_api.cooldown")
    @mock.patch("gophish_api.api_get")
    @mock.patch("gophish_api.api_post")
    def test_run_bulk_create_success(
        self, mock_post: mock.MagicMock, mock_get: mock.MagicMock,
        _mock_cd: mock.MagicMock, _mock_cdf: mock.MagicMock,
    ) -> None:
        from bulk_create_campaigns import run_bulk_create

        mock_get.return_value = {"campaigns": []}
        mock_post.return_value = {"id": 99, "name": "PROD Test 2026-05-26"}
        results, code = run_bulk_create(
            [
                {
                    "group_name": "Test Group",
                    "campaign_name": "PROD Test Group 2026-05-26",
                    "template_name": "T1",
                    "page_name": "P1",
                    "smtp_name": "SMTP1",
                }
            ],
            phishing_url="http://localhost",
            recheck=False,
        )
        self.assertEqual(code, 0)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].campaign_id, 99)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["groups"], [{"name": "Test Group"}])
        self.assertEqual(payload["smtp"], {"name": "SMTP1"})

    @mock.patch("gophish_bulk_config.cooldown_final")
    @mock.patch("gophish_api.cooldown")
    @mock.patch("gophish_api.api_get")
    def test_run_bulk_create_requires_smtp_per_row(
        self, mock_get: mock.MagicMock, _cd: mock.MagicMock, _cdf: mock.MagicMock
    ) -> None:
        from bulk_create_campaigns import run_bulk_create

        mock_get.return_value = {"campaigns": []}
        results, code = run_bulk_create(
            [{"group_name": "G", "template_name": "T", "page_name": "P"}],
            phishing_url="http://localhost",
            recheck=False,
        )
        self.assertEqual(code, 1)
        self.assertFalse(results[0].ok)
        self.assertIn("SMTP", results[0].errors[0])


class FlaskCampaignCreateTests(unittest.TestCase):
    def test_resources_requires_auth(self) -> None:
        _reload_gophish_api(GOPHISH_URL="", GOPHISH_API_KEY="")
        import gophish_manager

        importlib.reload(gophish_manager)
        client = gophish_manager.app.test_client()
        resp = client.get("/ajax/campaign-create/resources")
        self.assertEqual(resp.status_code, 401)

    @mock.patch("bulk_create_campaigns.fetch_create_resources")
    def test_resources_json(self, mock_fetch: mock.MagicMock) -> None:
        _reload_gophish_api(
            GOPHISH_URL="https://gophish.test:3333",
            GOPHISH_API_KEY="test-key",
        )
        mock_fetch.return_value = {
            "groups": [{"id": 1, "name": "G1", "num_users": 10}],
            "templates": [{"id": 1, "name": "T1"}],
            "pages": [{"id": 1, "name": "P1"}],
            "smtp_profiles": [{"id": 1, "name": "S1"}],
            "default_campaign_name": "PROD Example 2026-05-26",
        }
        import gophish_manager

        importlib.reload(gophish_manager)
        # Clear the server-side cache so the mock is actually called
        gophish_manager._RESOURCE_CACHE["data"] = None
        gophish_manager._RESOURCE_CACHE["ts"] = 0.0
        client = gophish_manager.app.test_client()
        resp = client.get("/ajax/campaign-create/resources?refresh=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["groups"]), 1)

    @mock.patch("bulk_create_campaigns.fetch_create_resources")
    def test_resources_served_from_cache(self, mock_fetch: mock.MagicMock) -> None:
        _reload_gophish_api(
            GOPHISH_URL="https://gophish.test:3333",
            GOPHISH_API_KEY="test-key",
        )
        import time
        import gophish_manager

        importlib.reload(gophish_manager)
        cached_data = {
            "groups": [], "templates": [], "pages": [], "smtp_profiles": [],
            "default_campaign_name": "PROD x",
        }
        gophish_manager._RESOURCE_CACHE["data"] = cached_data
        gophish_manager._RESOURCE_CACHE["ts"] = time.time()  # fresh
        client = gophish_manager.app.test_client()
        resp = client.get("/ajax/campaign-create/resources")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("cached"))
        mock_fetch.assert_not_called()


class BootstrapTests(unittest.TestCase):
    def test_venv_python_path_on_windows(self) -> None:
        import bootstrap

        with mock.patch("bootstrap.os.name", "nt"):
            path = bootstrap.venv_python()
        self.assertTrue(path.endswith("Scripts\\python.exe") or path.endswith("Scripts/python.exe"))

    def test_broken_copied_venv_is_not_usable(self) -> None:
        """A venv whose Python cannot execute here is reported unusable."""
        import bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            venv = os.path.join(tmp, "venv")
            subdir = "Scripts" if os.name == "nt" else "bin"
            os.makedirs(os.path.join(venv, subdir))
            # A non-executable placeholder stands in for a broken interpreter.
            open(os.path.join(venv, subdir, "python.exe"), "wb").close()
            open(os.path.join(venv, subdir, "python"), "wb").close()
            with mock.patch("bootstrap.venv_dir", return_value=venv):
                self.assertFalse(bootstrap._venv_usable())

    def test_current_interpreter_counts_as_running(self) -> None:
        import bootstrap

        self.assertTrue(bootstrap._python_runs(sys.executable))

    def test_real_interpreter_satisfies_dep_probe(self) -> None:
        import bootstrap

        with mock.patch.object(bootstrap, "REQUIRED_MODULES", ("os", "sys", "json")):
            self.assertTrue(bootstrap._deps_available_in(sys.executable))
            self.assertTrue(bootstrap.deps_available())

    def test_missing_module_fails_dep_probe(self) -> None:
        import bootstrap

        with mock.patch.object(
            bootstrap, "REQUIRED_MODULES", ("os", "definitely_not_a_real_module_xyz")
        ):
            self.assertFalse(bootstrap._deps_available_in(sys.executable))
            self.assertFalse(bootstrap.deps_available())

    def test_pip_attempts_in_venv_has_no_pep668_flags(self) -> None:
        import bootstrap

        attempts = bootstrap._pip_attempts("py", "req.txt", in_venv=True)
        # plain, then a trusted-host retry for corporate SSL proxies.
        self.assertEqual(len(attempts), 2)
        for cmd in attempts:
            self.assertNotIn("--user", cmd)
            self.assertNotIn("--break-system-packages", cmd)
        self.assertNotIn("--trusted-host", attempts[0])
        self.assertIn("--trusted-host", attempts[1])

    def test_pip_attempts_outside_venv_adds_pep668_and_ssl_fallbacks(self) -> None:
        import bootstrap

        attempts = bootstrap._pip_attempts("py", "req.txt", in_venv=False)
        self.assertEqual(len(attempts), 6)
        self.assertIn("--user", attempts[1])
        self.assertIn("--break-system-packages", attempts[2])
        # The first three have no SSL bypass; the rest do.
        self.assertFalse(any("--trusted-host" in c for c in attempts[:3]))
        self.assertTrue(all("--trusted-host" in c for c in attempts[3:]))

    def test_trusted_host_flags_cover_pypi_hosts(self) -> None:
        import bootstrap

        flags = bootstrap._trusted_host_flags()
        self.assertIn("pypi.org", flags)
        self.assertIn("files.pythonhosted.org", flags)

    def test_running_in_project_venv_false_for_system_python(self) -> None:
        import bootstrap

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("bootstrap.venv_dir", return_value=os.path.join(tmp, "venv")):
                self.assertFalse(bootstrap.running_in_project_venv())

    def test_bootstrap_no_op_when_deps_available(self) -> None:
        """If deps already import, bootstrap must not touch any venv."""
        import bootstrap

        with mock.patch.object(bootstrap, "running_in_project_venv", return_value=False):
            with mock.patch.object(bootstrap, "deps_available", return_value=True):
                with mock.patch.object(bootstrap, "ensure_usable_venv") as ensure:
                    with mock.patch.object(bootstrap, "pip_install_into") as pip:
                        bootstrap.bootstrap()
                        ensure.assert_not_called()
                        pip.assert_not_called()

    def test_bootstrap_falls_back_to_current_python_without_venv(self) -> None:
        """When no venv can be built, deps install into the current interpreter."""
        import bootstrap

        with mock.patch.object(bootstrap, "running_in_project_venv", return_value=False):
            with mock.patch.object(bootstrap, "deps_available", return_value=False):
                with mock.patch.object(bootstrap, "ensure_usable_venv", return_value=None):
                    with mock.patch.object(bootstrap, "pip_install_into") as pip:
                        bootstrap.bootstrap()
                        pip.assert_called_once()
                        self.assertEqual(pip.call_args.args[0], sys.executable)


if __name__ == "__main__":
    unittest.main()
