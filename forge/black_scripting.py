"""Versioned Black Scripting definitions over the established linear job queue.

The legacy ``workflows`` key and API are intentionally kept intact. Reading old
presets projects them into the new representation without rewriting their data.
Tool settings are snapshots: resolving a saved script never looks up tool presets.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .core import IMAGES, TOOLS, absolute_path
from .entitlements import require_feature
from .models import OPTION_MODELS, Output, Strict, Workflow


ID_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$'
STORE_KEY = 'black_scripting'


class OperationNode(Strict):
    id: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    type: Literal['operation'] = 'operation'
    node_version: Literal[1] = 1
    tool: Literal['retune', 'metadata', 'transcribe', 'watermark', 'audio_fingerprint',
                  'video_fingerprint', 'tracking_audit', 'privacy_export', 'provenance',
                  'privacy_verify']
    enabled: bool = True
    options: dict = Field(default_factory=dict)
    preset_name: str = Field('', max_length=80)

    @model_validator(mode='after')
    def validate_options(self):
        self.options = OPTION_MODELS[self.tool].model_validate(self.options).model_dump()
        return self


class Connection(Strict):
    source: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)
    target: str = Field(min_length=1, max_length=80, pattern=ID_PATTERN)


class Script(Strict):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=ID_PATTERN, max_length=80)
    name: str = Field('Untitled script', min_length=1, max_length=80)
    nodes: list[OperationNode] = Field(default_factory=list, max_length=24)
    connections: list[Connection] = Field(default_factory=list, max_length=23)
    output: Output = Field(default_factory=Output)

    @field_validator('name')
    @classmethod
    def printable_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Use a nonempty, printable script name.')
        return value

    @model_validator(mode='after')
    def linear_chain(self):
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError('Every Black Scripting operation needs a unique identifier.')
        expected = list(zip(ids, ids[1:]))
        actual = [(edge.source, edge.target) for edge in self.connections]
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise ValueError('Black Scripting supports one linear chain in node order. Branches, cycles, missing links and unknown identifiers are not supported.')
        active = self.active_nodes()
        if any(node.tool == 'transcribe' for node in active[:-1]):
            raise ValueError('Transcribe must be the last enabled operation because its outputs are text.')
        return self

    def active_nodes(self):
        return [node for node in self.nodes if node.enabled]


class PlanRequest(Strict):
    script: Script
    sources: list[str] = Field(min_length=1, max_length=200)


class RunRequest(PlanRequest):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]+$')


class ImportRequest(Strict):
    definition: dict


def from_legacy(value: dict) -> Script:
    """Stable identity lets saving an old preset supersede its projection only."""
    old = Workflow.model_validate(value)
    script_id = 'legacy-' + uuid.uuid5(uuid.NAMESPACE_URL, 'forge:workflow:' + old.name).hex
    nodes = [OperationNode(id=f'step-{i + 1}', tool=step.tool, options=step.options)
             for i, step in enumerate(old.steps)]
    return Script(id=script_id, name=old.name, nodes=nodes, output=old.output,
                  connections=[Connection(source=a.id, target=b.id) for a, b in zip(nodes, nodes[1:])])


def parse_definition(value: dict) -> Script:
    # JSON only: never execute, evaluate, or load arbitrary configuration classes.
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 512_000:
        raise ValueError('Black Scripting definitions must be smaller than 512 KB.')
    if 'schema_version' not in value and 'steps' in value:
        return from_legacy(value)
    return Script.model_validate(value)


def scripts_for_store(store):
    scripts = {}
    warnings = []
    for legacy in store.get('workflows', []):
        try:
            script = from_legacy(legacy)
            scripts[script.id] = script.model_dump()
        except (ValueError, TypeError):
            warnings.append('A legacy Black Scripting preset could not be loaded; its original stored definition is preserved.')
    for value in store.get(STORE_KEY, []):
        try:
            script = Script.model_validate(value)
            scripts[script.id] = script.model_dump()
        except (ValueError, TypeError):
            warnings.append('A saved Black Scripting preset uses an unsupported definition; its stored data is preserved.')
    return {'scripts': list(scripts.values()), 'warnings': warnings, 'schema_version': 1}


def mount(app, store, manager, destination):
    def save(script):
        with store.lock:
            values = store.get(STORE_KEY, [])
            index = next((i for i, value in enumerate(values) if isinstance(value, dict) and value.get('id') == script.id), None)
            if index is None:
                if len(values) >= 100:
                    raise ValueError('Up to 100 new Black Scripting presets can be stored. Existing presets have been preserved.')
                values.append(script.model_dump())
            else:
                values[index] = script.model_dump()
            store.set(STORE_KEY, values)
        return {'script': script.model_dump(), **scripts_for_store(store)}

    def prepare(body):
        from .media_api import validate_media_options
        from .resources import MEDIA_ANALYSIS
        require_feature(app, 'black_scripting')
        active = body.script.active_nodes()
        if not active:
            raise ValueError('Add and enable an operation before previewing the execution plan.')
        settings = store.settings()
        for node in active:
            require_feature(app, node.tool)
        plans = []
        for source_id in dict.fromkeys(body.sources):
            source = store.source(source_id)
            path = absolute_path(source['path'])
            ext = path.suffix.lower()
            steps = []
            assets = {}
            previous = 'source'
            for index, node in enumerate(active, 1):
                if ext not in TOOLS[node.tool]['accept']:
                    raise ValueError(f'{path.name}: {TOOLS[node.tool]["title"]} cannot accept the previous operation’s {ext} output.')
                if node.tool == 'metadata' and ext in IMAGES and node.options['mode'] != 'wipe':
                    raise ValueError(f'{path.name}: image metadata supports privacy cleaning; selective editing requires supported audio or video.')
                input_format = ext
                disclosures = []
                if node.tool in MEDIA_ANALYSIS | {'watermark', 'privacy_export'}:
                    # Validate local assets and the source contract up front.
                    # Downstream media does not exist yet; pass its planned
                    # suffix separately and let workers verify actual streams.
                    if index == 1 or all(step['passthrough'] for step in steps):
                        operation_plan = validate_media_options(node.tool, path, node.options, settings)
                    else:
                        operation_plan = validate_media_options(node.tool, path, node.options, settings,
                                                                input_suffix=ext, inspect_source=False)
                    ext = operation_plan.get('output_suffix', ext)
                    disclosures = operation_plan.get('disclosures', [])
                    assets[node.id] = operation_plan.get('asset_snapshots', [])
                if node.tool == 'retune':
                    ext = '.' + node.options['format']
                output_formats = ['.' + fmt for fmt in node.options['formats']] if node.tool == 'transcribe' else [ext]
                steps.append({'node_id': node.id, 'step': index, 'tool': node.tool,
                              'title': TOOLS[node.tool]['title'], 'depends_on': previous,
                              'input_format': input_format, 'output_formats': output_formats,
                              'preset_name': node.preset_name,
                              'passthrough': node.tool in MEDIA_ANALYSIS,
                              'disclosures': disclosures})
                previous = node.id
            stat = path.stat()
            plans.append({'source': source, 'steps': steps, 'assets': assets,
                          'snapshot': {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}})
        # Every file and media transition passes before resolving writable destinations.
        for plan in plans:
            plan['destination'] = str(destination(body.script.output, plan['source'], settings))
        fingerprint = {'script': body.script.model_dump(), 'plans': plans}
        token = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return active, settings, plans, token

    @app.get('/api/black-scripting/scripts')
    def list_scripts():
        return scripts_for_store(store)

    @app.post('/api/black-scripting/save')
    def save_script(body: Script):
        return save(body)

    @app.post('/api/black-scripting/import')
    def import_script(body: ImportRequest):
        script = parse_definition(body.definition)
        # Import is additive: even matching IDs must never overwrite a local preset.
        script.id = uuid.uuid4().hex
        return save(script)

    @app.post('/api/black-scripting/export')
    def export_script(body: Script):
        return {'definition': body.model_dump()}

    @app.post('/api/black-scripting/preview')
    def preview(body: PlanRequest):
        active, settings, plans, token = prepare(body)
        return {'preview_token': token, 'script_id': body.script.id, 'name': body.script.name,
                'jobs': len(plans) * len(active),
                'disabled': [node.id for node in body.script.nodes if not node.enabled],
                'sources': [{'source_id': item['source']['id'], 'name': item['source']['name'],
                             'destination': item['destination'], 'steps': item['steps']} for item in plans],
                'note': 'Transformations pass their published media to the next operation. Inspections pass the inspected media unchanged, including when private reports are disabled. Independent source chains may run concurrently. Existing files receive new, collision-safe names.'}

    @app.post('/api/black-scripting/run')
    def run(body: RunRequest):
        active, settings, plans, token = prepare(body)
        if not secrets.compare_digest(token, body.preview_token):
            raise ValueError('The script, source files or output destination changed. Preview the execution plan again.')
        batch = uuid.uuid4().hex
        jobs = []
        with manager.lock:
            manager.check_accepting()
            for plan in plans:
                previous = ''
                for index, node in enumerate(active, 1):
                    payload = {'tool': node.tool, 'options': node.options, 'settings': settings,
                               'destination': plan['destination'], 'source': plan['source'],
                               'source_stat': plan['snapshot'], 'url': None, 'depends_on': previous,
                               'original_source': plan['source'], 'original_source_stat': plan['snapshot'],
                               'asset_snapshots': plan['assets'].get(node.id, []),
                               'chain_operations': [operation.tool for operation in active[:index - 1]],
                               'batch_id': batch, 'workflow_name': body.script.name, 'step': index,
                               'steps': len(active), 'script_id': body.script.id, 'node_id': node.id,
                               'script_schema_version': body.script.schema_version}
                    job = store.add_job(node.tool, plan['source']['name'], payload)
                    previous = job['id']
                    jobs.append({key: value for key, value in job.items() if key != 'payload'})
        if body.script.output.mode == 'custom':
            settings['last_output'] = body.script.output.directory
            store.set('settings', settings)
        return {'jobs': jobs, 'batch_id': batch}
