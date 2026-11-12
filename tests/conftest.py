"""Suite-wide guards."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_ntfy_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ntfy topic lives in the developer's `.env` / environment
    (`notify._topic`). A test config without `ntfy_topic` must stay
    topic-less instead of picking that up and pushing to the real channel."""
    import notify

    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(notify, "_topic_from_dotenv", lambda: None)
