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

TABS = {
    "type": "array",
    "description": "Formatted workbook tabs with typed columns and literal cell values.",
    "minItems": 1,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "columns", "rows"],
        "properties": {
            "name": text("Tab name."),
            "heading": text("Optional title row."),
            "note": text("Optional note row."),
            "columns": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["header", "type"],
                    "properties": {
                        "header": text("Column heading."),
                        "type": {
                            "type": "string",
                            "enum": [
                                "text",
                                "number",
                                "money",
                                "percent",
                                "date",
                                "datetime",
                                "bool",
                                "url",
                            ],
                        },
                        "decimals": {"type": "integer", "minimum": 0, "maximum": 4},
                        "total": {"type": "boolean"},
                        "width": {"type": "integer", "minimum": 20, "maximum": 1000},
                    },
                },
            },
            "rows": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": ["string", "number", "boolean", "null"]},
                },
            },
        },
    },
}

SPAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {
        "text": text("Nonempty text within one paragraph."),
        "bold": {"type": "boolean"},
        "italic": {"type": "boolean"},
        "underline": {"type": "boolean"},
        "code": {"type": "boolean"},
        "link": text("HTTP or HTTPS URL with a host."),
    },
}

BLOCKS = {
    "type": "array",
    "description": "Structured Google Doc content blocks.",
    "minItems": 1,
    "maxItems": 200,
    "items": {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {"type": {"const": "divider", "description": "Horizontal divider."}},
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "items"],
                "properties": {
                    "type": {"enum": ["bullets", "numbered"]},
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 100,
                                    "items": SPAN_SCHEMA,
                                },
                            ]
                        },
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "header", "rows"],
                "properties": {
                    "type": {"const": "table"},
                    "header": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string"},
                    },
                    "rows": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {"type": "array", "items": {"type": ["string", "null"]}},
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "oneOf": [
                    {"required": ["text"], "not": {"required": ["spans"]}},
                    {"required": ["spans"], "not": {"required": ["text"]}},
                ],
                "properties": {
                    "type": {"enum": ["title", "paragraph"]},
                    "text": {"type": "string"},
                    "spans": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": SPAN_SCHEMA,
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "level"],
                "oneOf": [
                    {"required": ["text"], "not": {"required": ["spans"]}},
                    {"required": ["spans"], "not": {"required": ["text"]}},
                ],
                "properties": {
                    "type": {"const": "heading"},
                    "level": {"type": "integer", "minimum": 1, "maximum": 3},
                    "text": {"type": "string"},
                    "spans": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": SPAN_SCHEMA,
                    },
                },
            },
        ]
    },
}


def parameters(properties, required=()):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
    }
