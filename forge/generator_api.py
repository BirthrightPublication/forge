"""Generator routes deliberately bypass the job manager and filesystem."""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from typing import Literal

from .generators import GeneratorID, catalog, export_result, generate, validate_options
from .entitlements import require_feature


class Request(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    tool: GeneratorID
    options: dict = Field(default_factory=dict)


class ExportRequest(Request):
    format: Literal['json', 'csv', 'txt'] = 'json'


class PresetRequest(Request):
    name: str = Field(min_length=1, max_length=60)
    id: str | None = Field(None, pattern=r'^[a-f0-9]{32}$')

    @field_validator('name')
    @classmethod
    def title(cls, value):
        value = value.strip()
        if not value or any(not c.isprintable() for c in value):
            raise ValueError('Use a visible preset name without control characters.')
        return value


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-f0-9]{32}$')


def checked(call, *args):
    try:
        return call(*args)
    except ValidationError as exc:
        # Do not echo submitted field contents or exception context in responses.
        detail = '; '.join('.'.join(map(str, error['loc'])) + ': ' + error['msg']
                           for error in exc.errors(include_input=False, include_context=False))
        raise HTTPException(422, detail=detail) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


def mount(app, store, manager=None):
    @app.get('/api/generators/catalog')
    def generator_catalog():
        return JSONResponse(catalog(), headers={'Cache-Control': 'no-store'})

    @app.post('/api/generators/generate')
    def generator_generate(body: Request):
        require_feature(app, body.tool)
        result = checked(generate, body.tool, body.options)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/api/generators/export')
    def generator_export(body: ExportRequest):
        # API clients may explicitly export a new batch without creating a job.
        # The browser exports its current batch via a local Blob instead.
        require_feature(app, body.tool)
        result = checked(generate, body.tool, body.options)
        text = export_result(result, body.format)
        mime = {'json': 'application/json', 'csv': 'text/csv', 'txt': 'text/plain'}[body.format]
        return Response(text, media_type=mime, headers={'Cache-Control': 'no-store',
                        'Content-Disposition': f'attachment; filename="FORGE-{body.tool}.{body.format}"'})

    @app.get('/api/generators/presets')
    def generator_presets():
        return {'presets': store.get('generator_presets', [])}

    @app.post('/api/generators/presets')
    def generator_save_preset(body: PresetRequest):
        options = checked(validate_options, body.tool, body.options).model_dump()
        with store.lock:
            presets = store.get('generator_presets', [])
            previous = next((p for p in presets if (p['id'] == body.id if body.id else p['tool'] == body.tool and p['name'] == body.name)), None)
            if body.id and not previous:
                raise HTTPException(404, 'Preset was not found.')
            if previous and previous['tool'] != body.tool:
                raise HTTPException(422, 'A preset cannot change generator type.')
            if any(p['tool'] == body.tool and p['name'] == body.name and p != previous for p in presets):
                raise HTTPException(409, 'A preset with that name already exists.')
            if not previous and len(presets) >= 100:
                raise HTTPException(422, 'Keep at most 100 generator presets. Remove an unused preset first.')
            preset = {'id': previous['id'] if previous else uuid.uuid4().hex, 'name': body.name,
                      'tool': body.tool, 'options': options, 'schema_version': 1}
            presets = [p for p in presets if p != previous] + [preset]
            store.set('generator_presets', presets)
        return {'preset': preset, 'presets': presets}

    @app.post('/api/generators/presets/delete')
    def generator_delete_preset(body: DeleteRequest):
        with store.lock:
            presets = store.get('generator_presets', [])
            if not any(p['id'] == body.id for p in presets):
                raise HTTPException(404, 'Preset was not found.')
            presets = [p for p in presets if p['id'] != body.id]
            store.set('generator_presets', presets)
        return {'presets': presets}
