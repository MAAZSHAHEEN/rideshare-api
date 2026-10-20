"""Public request errors contain schema guidance, never submitted input/context."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


# Generate messages from trusted text rather than validator exceptions: even msg
# can contain input (email errors, custom validators, discriminated unions).
_MESSAGES = {
    "missing": "Field required",
    "string_type": "Input should be a valid string",
    "int_type": "Input should be a valid integer",
    "int_parsing": "Input should be a valid integer, unable to parse string as an integer",
    "int_from_float": "Input should be a valid integer, got a number with a fractional part",
    "model_attributes_type": "Input should be a valid dictionary or object to extract fields from",
    "model_type": "Input should be a valid dictionary",
    "dict_type": "Input should be a valid dictionary",
    "list_type": "Input should be a valid list",
    "json_invalid": "Invalid JSON",
    "literal_error": "Input should be one of the permitted values",
    "enum": "Input should be one of the permitted values",
    "union_tag_invalid": "Input should use a permitted discriminator value",
    "union_tag_not_found": "Required discriminator field is missing",
    "datetime_type": "Input should be a valid datetime",
    "datetime_parsing": "Input should be a valid datetime",
    "datetime_from_date_parsing": "Input should be a valid datetime or date",
    "timezone_aware": "Input should have timezone info",
    "extra_forbidden": "Extra inputs are not permitted",
}
_BOUNDS = {
    "string_too_short": ("min_length", "String should have at least {} characters"),
    "string_too_long": ("max_length", "String should have at most {} characters"),
    "greater_than": ("gt", "Input should be greater than {}"),
    "greater_than_equal": ("ge", "Input should be greater than or equal to {}"),
    "less_than": ("lt", "Input should be less than {}"),
    "less_than_equal": ("le", "Input should be less than or equal to {}"),
}
_PASSWORD_MESSAGES = {
    "Value error, Password must not be blank",
    "Value error, Password must be at most 72 UTF-8 bytes",
}


def _message(error: dict) -> str:
    kind = error["type"]
    if kind == "value_error":
        message = error.get("msg", "")
        if message in _PASSWORD_MESSAGES:
            return message
        if message.startswith("value is not a valid email address"):
            return "Value is not a valid email address"
    if kind in _BOUNDS:
        key, template = _BOUNDS[kind]
        bound = error.get("ctx", {}).get(key)
        if type(bound) in (int, float):
            return template.format(bound)
    return _MESSAGES.get(kind, "Invalid value")


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError,
) -> JSONResponse:
    # Allowlist output keys. Do not serialize input, body, ctx, or raw exceptions.
    return JSONResponse(status_code=422, content={"detail": [
        {"type": error["type"], "loc": list(error["loc"]), "msg": _message(error)}
        for error in exc.errors()
    ]})
