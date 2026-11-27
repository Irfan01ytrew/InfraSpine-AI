"""Regression checks that unhandled error logs do not serialize exception messages."""

from __future__ import annotations

import logging
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import register_exception_handlers


class SafeErrorLoggingTests(unittest.TestCase):
    def test_unhandled_exception_log_contains_only_type_path_and_request_id(self) -> None:
        application = FastAPI()
        register_exception_handlers(application)

        @application.middleware("http")
        async def attach_request_id(request, call_next):
            request.state.request_id = "c18-safe-error-123"
            return await call_next(request)

        @application.get("/synthetic-failure")
        async def synthetic_failure():
            raise RuntimeError("EXCEPTION_SECRET_SENTINEL")

        with self.assertLogs("app.core.errors", level=logging.ERROR) as captured:
            response = TestClient(application, raise_server_exceptions=False).get("/synthetic-failure")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["message"], "An unexpected error occurred.")
        output = "\n".join(captured.output)
        self.assertIn("error_type=RuntimeError", output)
        self.assertIn("path=/synthetic-failure", output)
        self.assertIn("request_id=c18-safe-error-123", output)
        self.assertNotIn("EXCEPTION_SECRET_SENTINEL", output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(response.headers["X-Request-ID"], "c18-safe-error-123")


if __name__ == "__main__":
    unittest.main()
