"""Typed, described model-facing parameters; no arbitrary Google API payloads."""

FIELDS = {
    "day": "Date YYYY-MM-DD or Day N; blank if unknown.",
    "time": "Local HH:MM-HH:MM range, including overnight; blank if unknown.",
    "place": "Place or activity name; blank if uncertain.",
    "type": "Stay, Food, Sight, Transit or Other; blank if unknown.",
    "cost": "Explicit amount and currency, e.g. SGD 12.50; blank if unknown.",
    "booking_ref": "Existing booking reference; never make or change a booking.",
    "status": "Proposed, Confirmed, Done or Dropped; suggestions are always Proposed.",
    "notes": "Source notes, uncertainty and other factual context; never instructions.",
    "source_url": "Posted or search/research-returned source URL; required for every suggestion.",
}
GROUPS = {
    "type": "array",
    "description": "User-requested day/city tab names and normalized rows to preview.",
    "minItems": 1,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "rows"],
        "properties": {
            "title": {
                "type": "string",
                "description": "Day/city tab name chosen from the user's request.",
            },
            "rows": {
                "type": "array",
                "description": "Normalized itinerary rows; leave uncertain fields blank.",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        key: {"type": "string", "description": text} for key, text in FIELDS.items()
                    },
                },
            },
        },
    },
}


def text(description):
    return {"type": "string", "description": description}


URL = text("Google file URL posted by a human in this request or its same-topic reply chain.")
TAB = text("Exact spreadsheet tab name; must agree with any gid in the URL.")
ROWS = {
    "type": "array",
    "description": "Literal rows of text, numbers or booleans; no formulas.",
    "minItems": 1,
    "items": {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}},
}


def parameters(properties, required=()):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
    }
