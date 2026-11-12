"""Имена копий баз подставляются прямо в SQL — проверяем, что туда не попадёт лишнее."""

import pytest

from webpsql.sandbox import DB_PREFIX, ROLE_PREFIX, _names

SID = "0123456789abcdef0123456789abcdef"


def test_names_for_valid_session():
    assert _names(SID) == (DB_PREFIX + SID, ROLE_PREFIX + SID)


def test_names_fit_postgres_limit():
    """PostgreSQL обрезает имена длиннее 63 байт — две сессии не должны получить одно имя."""
    database, role = _names(SID)
    assert len(database) <= 63
    assert len(role) <= 63


@pytest.mark.parametrize("sid", [
    "",
    "short",
    SID.upper(),                      # только строчные hex, как у uuid4().hex
    SID[:-1] + "g",
    SID + "0",
    "x; drop database postgres; --",
    SID[:16] + "'; select 1; --",
])
def test_names_reject_anything_else(sid):
    with pytest.raises(ValueError):
        _names(sid)
