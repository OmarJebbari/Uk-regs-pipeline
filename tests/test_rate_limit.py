"""Tests for rate-limit config + search query validation (§8)."""

import pytest
from uk_regs_api.ratelimit import validate_search_query


class TestSearchQueryValidation:
    def test_valid_query_passes_through(self):
        assert validate_search_query("fire door rating") == "fire door rating"

    def test_strips_surrounding_whitespace(self):
        assert validate_search_query("  fire safety  ") == "fire safety"

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="empty"):
            validate_search_query("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="empty"):
            validate_search_query("   \t\n  ")

    def test_rejects_oversized_query(self):
        # Max is 2000 by default; send 2001 chars
        huge = "x" * 2001
        with pytest.raises(ValueError, match="max length"):
            validate_search_query(huge)

    def test_strips_control_characters(self):
        # null byte + bell should be removed, text retained
        dirty = "fire\x00door\x07rating"
        assert validate_search_query(dirty) == "firedoorrating"

    def test_preserves_allowed_whitespace(self):
        # tabs and newlines are allowed control chars
        assert validate_search_query("fire\tdoor\nrating") == "fire\tdoor\nrating"


class TestRateLimitConfig:
    def test_settings_have_rate_limit_defaults(self):
        from uk_regs_core import get_settings

        settings = get_settings()
        assert "minute" in settings.ratelimit_global
        assert "minute" in settings.ratelimit_search
        assert "minute" in settings.ratelimit_mutate
        assert settings.search_max_query_chars > 0
