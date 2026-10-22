import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from shared.errors import AgentError


class GoogleAuth(httpx.Auth):
    def __init__(self, credentials: Credentials):
        self.credentials = credentials

    def auth_flow(self, request):
        if not self.credentials.valid:
            try:
                self.credentials.refresh(Request())
            except Exception:
                raise AgentError("google_auth", "Google OAuthの再認証が必要です。") from None
        request.headers["Authorization"] = f"Bearer {self.credentials.token}"
        yield request
