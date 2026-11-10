"""Tests run without WhisperX or Nextcloud: they cover the web app, permissions and the deploy check."""
import os
import sys
import tempfile
from pathlib import Path

_data = tempfile.mkdtemp(prefix="ttrpg-test-")
os.environ.update(DATA_DIR=_data, ADMIN_USERNAME="admin", ADMIN_PASSWORD="test-password-123",
                  SESSION_COOKIE_SECURE="false", NEXTCLOUD_URL="", OIDC_ISSUER="")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
