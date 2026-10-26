"""Tests for the Prompt Optimizer API.

Run with: python3 -m pytest test_app.py -v
      or: python3 -m unittest -v test_app

No test needs a database, an API key or a network. The two deployment fixes
this project is about are both tested directly: the app boots with an
unreachable database, and the Dockerfile uses a shell form CMD so $PORT
actually expands.
"""

import os
import unittest

from fastapi.testclient import TestClient

import config
import db
import main
import service


class APITestCase(unittest.TestCase):
    def setUp(self):
        db.reset_for_tests()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        db.reset_for_tests()


class TestHealth(APITestCase):

    def test_health_is_200_without_a_database(self):
        # A platform health check asks "should I restart this container?".
        # A missing database is not a reason to, because /optimize still works.
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("unavailable", body["database"])

    def test_health_reports_which_optimizer_is_live(self):
        body = self.client.get("/health").json()
        self.assertIn(body["llm"], ("configured", "rules fallback"))

    def test_health_response_forbids_extra_fields(self):
        self.assertEqual(sorted(self.client.get("/health").json()),
                         ["database", "llm", "status", "version"])


class TestOptimize(APITestCase):

    def test_happy_path(self):
        response = self.client.post(
            "/optimize", json={"prompt": "explain caching", "style": "concise"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["original"], "explain caching")
        self.assertTrue(body["optimized"])
        self.assertEqual(body["source"], "rules")

    def test_style_defaults_to_concise(self):
        body = self.client.post("/optimize",
                                json={"prompt": "explain caching"}).json()
        self.assertEqual(body["style"], "concise")

    def test_a_typo_in_a_field_name_is_a_422_not_a_silent_success(self):
        """The reason the models forbid extras.

        Without extra='forbid', this body is accepted, 'prompt' falls back to
        its default or errors obscurely, and the caller gets a 200 with an
        answer to a question they did not ask.
        """
        response = self.client.post("/optimize",
                                    json={"promt": "typo", "style": "concise"})
        self.assertEqual(response.status_code, 422)

    def test_an_unexpected_extra_field_is_rejected(self):
        response = self.client.post(
            "/optimize",
            json={"prompt": "hi", "style": "concise", "temperature": 0.9})
        self.assertEqual(response.status_code, 422)
        self.assertIn("extra", str(response.json()).lower())

    def test_empty_prompt_rejected(self):
        self.assertEqual(
            self.client.post("/optimize", json={"prompt": ""}).status_code, 422)

    def test_whitespace_only_prompt_rejected(self):
        response = self.client.post("/optimize", json={"prompt": "    "})
        self.assertEqual(response.status_code, 422)

    def test_unknown_style_is_422_and_lists_the_valid_ones(self):
        response = self.client.post(
            "/optimize", json={"prompt": "hi", "style": "shakespearean"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("concise", str(response.json()))

    def test_overlong_prompt_rejected(self):
        response = self.client.post("/optimize",
                                    json={"prompt": "x" * 5000})
        self.assertEqual(response.status_code, 422)

    def test_upstream_failure_is_502_not_500(self):
        """502 says the fault was upstream and a retry may work. 500 does not."""
        def explode(prompt, style="concise"):
            raise service.UpstreamError("provider exploded")

        original = main.service.optimize
        main.service.optimize = explode
        try:
            response = self.client.post("/optimize", json={"prompt": "hi"})
        finally:
            main.service.optimize = original
        self.assertEqual(response.status_code, 502)

    def test_unexpected_failure_is_500_and_leaks_nothing(self):
        def explode(prompt, style="concise"):
            raise RuntimeError("secret internal detail")

        original = main.service.optimize
        main.service.optimize = explode
        try:
            response = self.client.post("/optimize", json={"prompt": "hi"})
        finally:
            main.service.optimize = original
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret internal detail", response.text)


class TestHistory(APITestCase):

    def test_history_without_a_database_explains_itself(self):
        response = self.client.get("/history")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["items"], [])
        self.assertIn("database", body["note"].lower())

    def test_history_limit_is_validated(self):
        self.assertEqual(self.client.get("/history?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/history?limit=500").status_code, 422)

    def test_history_round_trips_through_sqlite(self):
        self.assertTrue(db.init_db("sqlite:///./_test_history.db"))
        try:
            self.client.post("/optimize", json={"prompt": "explain caching"})
            body = self.client.get("/history").json()
            self.assertEqual(len(body["items"]), 1)
            self.assertEqual(body["items"][0]["original"], "explain caching")
        finally:
            db.reset_for_tests()
            if os.path.exists("_test_history.db"):
                os.remove("_test_history.db")


class TestDatabaseDegradation(unittest.TestCase):
    """init_db must never raise, whatever it is handed."""

    def tearDown(self):
        db.reset_for_tests()

    def test_no_url(self):
        self.assertFalse(db.init_db(""))
        self.assertIn("not set", db.status())

    def test_unparseable_url(self):
        self.assertFalse(db.init_db("this is not a url"))

    def test_unreachable_host(self):
        self.assertFalse(
            db.init_db("postgresql://u:p@no-such-host-12345:5432/d"))

    def test_record_without_a_database_returns_false_rather_than_raising(self):
        db.reset_for_tests()
        self.assertFalse(db.record("a", "b", "concise", "rules"))

    def test_history_without_a_database_is_empty_not_an_error(self):
        db.reset_for_tests()
        self.assertEqual(db.history(), [])


class TestRulesOptimizer(unittest.TestCase):

    def test_stacked_vague_openers_are_all_removed(self):
        # "please can you" is two openers. Stopping after the first leaves
        # "Can you ..." in place, which is the bug this catches.
        text, source = service.optimize("please can you explain caching")
        self.assertFalse(text.lower().startswith("can you"))
        self.assertTrue(text.startswith("Explain"))
        self.assertEqual(source, "rules")

    def test_output_is_punctuated(self):
        text, _ = service.optimize("explain caching")
        self.assertTrue(text.rstrip().endswith("."))

    def test_an_example_is_not_requested_twice(self):
        text, _ = service.optimize("give an example of recursion")
        self.assertEqual(text.lower().count("example"), 1)

    def test_styles_differ(self):
        concise, _ = service.optimize("explain caching", "concise")
        detailed, _ = service.optimize("explain caching", "detailed")
        self.assertNotEqual(concise, detailed)
        self.assertIn("step by step", detailed)

    def test_unknown_style_raises_value_error(self):
        with self.assertRaises(ValueError):
            service.optimize("hi", "nonsense")

    def test_whitespace_is_collapsed(self):
        text, _ = service.optimize("explain    caching\n\nplease")
        self.assertNotIn("  ", text)


class TestDeploymentReadiness(unittest.TestCase):
    """The two fixes this guided project is actually about."""

    @staticmethod
    def dockerfile_instructions():
        """Parse the Dockerfile, joining backslash continuations first.

        Needed because HEALTHCHECK's own body begins with the word CMD on a
        continuation line, so a naive line scan finds two CMD instructions and
        cannot tell which one actually starts the container.
        """
        with open("Dockerfile") as handle:
            raw = handle.read()
        joined = raw.replace("\\\n", " ")
        instructions = []
        for line in joined.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            instructions.append(line)
        return instructions

    @classmethod
    def dockerfile_instruction(cls, keyword):
        found = [line for line in cls.dockerfile_instructions()
                 if line.split(" ", 1)[0].upper() == keyword]
        assert len(found) == 1, "expected one %s, found %d" % (keyword, len(found))
        return found[0]

    def test_port_is_read_from_the_environment(self):
        import importlib
        saved = os.environ.get("PORT")
        os.environ["PORT"] = "10000"
        try:
            importlib.reload(config)
            self.assertEqual(config.PORT, 10000)
        finally:
            if saved is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = saved
            importlib.reload(config)

    def test_port_falls_back_to_8000(self):
        import importlib
        saved = os.environ.pop("PORT", None)
        try:
            importlib.reload(config)
            self.assertEqual(config.PORT, 8000)
        finally:
            if saved is not None:
                os.environ["PORT"] = saved
            importlib.reload(config)

    def test_dockerfile_cmd_is_shell_form(self):
        """Exec form would pass the literal string '$PORT' to uvicorn.

        uvicorn then exits with "Invalid value for '--port'", the platform
        restarts the container, and the deploy fails as a crash loop. The only
        thing standing between that and a working deploy is the absence of
        JSON brackets on this one line.
        """
        cmd = self.dockerfile_instruction("CMD")
        self.assertNotIn("[", cmd, "exec form would not expand $PORT")
        self.assertIn("${PORT", cmd, "the port must come from the environment")
        self.assertIn(":-8000", cmd, "there must be a local fallback")

    def test_dockerfile_runs_as_non_root(self):
        with open("Dockerfile") as handle:
            content = handle.read()
        self.assertIn("USER appuser", content)
        self.assertLess(content.index("USER appuser"), content.index("CMD"),
                        "USER must come before CMD or the app runs as root")

    def test_dockerfile_is_multi_stage(self):
        with open("Dockerfile") as handle:
            content = handle.read()
        self.assertGreaterEqual(content.count("FROM "), 2)
        self.assertIn("--from=builder", content)

    def test_compose_waits_for_the_database_to_be_healthy(self):
        import yaml
        with open("docker-compose.yml") as handle:
            compose = yaml.safe_load(handle)
        self.assertEqual(
            compose["services"]["api"]["depends_on"]["db"]["condition"],
            "service_healthy")
        self.assertIn("healthcheck", compose["services"]["db"])

    def test_no_secret_is_committed(self):
        for name in (".env.example", "docker-compose.yml"):
            with open(name) as handle:
                content = handle.read()
            self.assertNotIn("sk-", content)

    def test_env_file_is_gitignored(self):
        with open(".gitignore") as handle:
            self.assertIn(".env", handle.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
