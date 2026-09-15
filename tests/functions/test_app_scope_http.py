from __future__ import annotations

import json
import unittest

from core.app_scope import AppScopeEmptyError, AppScopeInitializingError
from functions._app_scope_http import app_scope_error_response


class AppScopeHttpTests(unittest.TestCase):
    def test_initializing_error_returns_retryable_response(self):
        response = app_scope_error_response(AppScopeInitializingError("Snapshot is warming."))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(503, response.status_code)
        self.assertEqual(
            {"error": "app_scope_initializing", "message": "Snapshot is warming."},
            json.loads(response.get_body()),
        )

    def test_empty_error_returns_configuration_response(self):
        response = app_scope_error_response(AppScopeEmptyError("Scope is empty."))

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(409, response.status_code)
        self.assertEqual(
            {"error": "app_scope_empty", "message": "Scope is empty."},
            json.loads(response.get_body()),
        )

    def test_unrelated_error_is_not_mapped(self):
        self.assertIsNone(app_scope_error_response(ValueError("other")))


if __name__ == "__main__":
    unittest.main()