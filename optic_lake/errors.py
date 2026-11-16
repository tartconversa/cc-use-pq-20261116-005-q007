class LakeError(Exception):
    code = "lake_error"
    status = 422

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details

    def json(self):
        return {"error": self.code, "message": str(self), "details": self.details}


class Invalid(LakeError):
    code = "invalid_input"


class Missing(LakeError):
    code = "not_found"
    status = 404


class Conflict(LakeError):
    code = "conflict"
    status = 409


class Stale(Conflict):
    code = "stale_state"


def check(condition, message, **details):
    if not condition:
        raise Invalid(message, **details)

