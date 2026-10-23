"""Working configuration, deliberately separate from presets and media payloads."""
from typing import Annotated
from functools import lru_cache

from pydantic import TypeAdapter, ValidationError

from .models import OPTION_MODELS, Output, Password
from .generators import MODELS

MODELS = {**OPTION_MODELS, **MODELS, 'password': Password}
# Only these constrained configuration strings are eligible beyond schema enums.
# No text/URL/contact/Wi-Fi/prompt/cookie/asset/reference or generated-value fields.
SAFE_STRINGS = {'language', 'model_revision', 'diarization_model', 'subtitle_languages',
                'foreground', 'background', 'color', 'symbols', 'exclude', 'custom_chars', 'pattern', 'remove_tags', 'filename_template'}


def _safe_shape(schema):
    if 'enum' in schema or 'const' in schema or schema.get('type') in {'boolean', 'integer', 'number', 'null'}:
        return True
    if schema.get('type') == 'array':
        return _safe_shape(schema.get('items', {}))
    return bool(schema.get('anyOf')) and all(_safe_shape(item) for item in schema['anyOf'])


@lru_cache(maxsize=1)
def allowed_fields():
    return {tool: [key for key, schema in model.model_json_schema().get('properties', {}).items()
                   if key != 'allow_model_download' and (key in SAFE_STRINGS or _safe_shape(schema))]
            for tool, model in MODELS.items()}


def safe_options(tool, options):
    if tool not in MODELS or not isinstance(options, dict):
        raise ValueError('Unknown tool configuration.')
    model = MODELS[tool]
    properties = model.model_json_schema().get('properties', {})
    values = {}
    for key, value in options.items():
        field = model.model_fields.get(key)
        if not field or key == 'allow_model_download':
            continue
        if key not in SAFE_STRINGS and not _safe_shape(properties.get(key, {})):
            continue
        try:
            values[key] = TypeAdapter(Annotated[field.annotation, field]).validate_python(value)
            if key == 'filename_template':
                from .download_support import validate_template
                values[key] = validate_template(values[key])
        except (ValidationError, ValueError):
            # Editing an incomplete field must not discard other valid preferences.
            continue
    return values


def mount(app, store):
    @app.get('/api/preferences')
    def preferences():
        return {'tools': store.get('working_preferences', {})}

    @app.post('/api/preferences/{tool}')
    def save(tool: str, body: dict):
        options = safe_options(tool, body.get('options', {}))
        output = Output.model_validate(body.get('output', {})).model_dump()
        with store.lock:
            values = store.get('working_preferences', {})
            values[tool] = {'options': options, 'output': output}
            store.set('working_preferences', values)
        return values[tool]

    @app.post('/api/preferences/{tool}/reset')
    def reset(tool: str):
        if tool not in MODELS:
            raise ValueError('Unknown tool.')
        with store.lock:
            values = store.get('working_preferences', {})
            values.pop(tool, None)
            store.set('working_preferences', values)
        return {'reset': True}
