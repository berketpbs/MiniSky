"""
MiniSky SDK Exceptions
"""


class MiniSkyError(Exception):
    """Base exception for MiniSky SDK."""
    pass


class APIError(MiniSkyError):
    """API request error."""
    def __init__(self, status_code: int, message: str, detail: str = None):
        self.status_code = status_code
        self.message = message
        self.detail = detail
        super().__init__(f"API Error {status_code}: {message}")


class ConnectionError(MiniSkyError):
    """Connection error."""
    pass


class TimeoutError(MiniSkyError):
    """Timeout error."""
    pass
