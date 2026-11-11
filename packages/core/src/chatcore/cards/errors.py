"""Safe errors shared by card validation and rendering."""


class CardError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        if code not in {"invalid", "busy", "too_large", "timeout", "unavailable"}:
            raise ValueError("invalid card error code")
        self.code = code
        self.detail = detail
        super().__init__(detail or code)
