"""Carry upstream error bytes and HTTP metadata through the real test gateway."""


class ProviderHTTPError(RuntimeError):
    def __init__(self, response):
        self.status_code = response.status_code
        self.body = response.content
        excluded = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "content-length",
            "content-encoding",
        }
        excluded.update(
            name.strip().lower() for name in response.headers.get("connection", "").split(",")
        )
        self.headers = {
            name: value for name, value in response.headers.items() if name.lower() not in excluded
        }
        super().__init__(f"Real DeepSeek request failed: HTTP {self.status_code}")
