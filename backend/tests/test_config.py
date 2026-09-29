from pathlib import Path

import pytest

from sentinel.config import configured_database_url


def test_database_url_can_be_resolved_from_a_file_mounted_password(tmp_path: Path):
    password_file = tmp_path / "postgres_password"
    password_file.write_text("contains / spaces? safely\n", encoding="utf-8")

    result = configured_database_url(
        {
            "DATABASE_PASSWORD_FILE": str(password_file),
            "DATABASE_HOST": "database.internal",
            "DATABASE_PORT": "5433",
            "DATABASE_NAME": "sentinel data",
            "DATABASE_USER": "sentinel user",
        }
    )

    assert result == "postgresql://sentinel%20user:contains%20%2F%20spaces%3F%20safely@database.internal:5433/sentinel%20data"


def test_explicit_database_url_takes_precedence_over_a_password_file(tmp_path: Path):
    result = configured_database_url(
        {
            "DATABASE_URL": "postgresql://explicit",
            "DATABASE_PASSWORD_FILE": str(tmp_path / "missing"),
        }
    )

    assert result == "postgresql://explicit"


def test_password_file_must_be_present_and_non_empty(tmp_path: Path):
    with pytest.raises(RuntimeError, match="could not be read"):
        configured_database_url({"DATABASE_PASSWORD_FILE": str(tmp_path / "missing")})

    password_file = tmp_path / "empty"
    password_file.write_text("\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="must contain"):
        configured_database_url({"DATABASE_PASSWORD_FILE": str(password_file)})
