"""Queue option contracts for the local speech production tools."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class SpeechOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    engine: str = Field('kokoro', max_length=80)
    model: str = Field('', max_length=160)
    voice: str = Field('am_onyx', max_length=160)
    revision: str = Field('', max_length=80)
    model_revision: str = Field('',pattern=r'^(|[0-9a-f]{40})$')
    voice_revision: int = Field(0,ge=0)
    device: Literal['auto','cpu','cuda'] = 'cpu'
    language: str = Field('en', max_length=40)
    text: str = Field('', max_length=500000)
    project_id: str = Field('', max_length=80)
    chapter_id: str = Field('', max_length=80)
    name: str = Field('speech', max_length=100)
    format: Literal['wav','flac','mp3','opus','m4b'] = 'wav'
    speed: float = Field(1, ge=.25, le=4)
    pitch: float = Field(0, ge=-24, le=24)
    volume: float = Field(0, ge=-60, le=24)
    blend: list[dict] = Field(default_factory=list, max_length=64)
    task: str = Field('voice', max_length=80)
    emotion: str = Field('neutral', max_length=80)
    instructions: str = Field('', max_length=4000)
    reference: str = Field('', max_length=2000)
    reference_text: str = Field('', max_length=10000)
    reference_audio: str = Field('', max_length=2000)
    attribute_speed: str = Field('', max_length=80)
    attribute_pitch: str = Field('', max_length=80)
    attribute_volume: str = Field('', max_length=80)
    dialect: str = Field('', max_length=80)
    consent: bool = False
    style: str | dict = 'normal'
    pronunciation: dict = Field(default_factory=dict)
    pauses: dict = Field(default_factory=dict)
    speakers: dict = Field(default_factory=dict)
    voice_effects: list[dict] = Field(default_factory=list, max_length=32)
    segment_effects: list[dict] = Field(default_factory=list, max_length=32)
    master_effects: list[dict] = Field(default_factory=list, max_length=32)
    metadata: dict = Field(default_factory=dict)
    scope: Literal['selection','chapter','document','preview'] = 'document'
    separate_chapters: bool = False
    combined: bool = True
    cover: str = Field('', max_length=2000)
    emotion_profiles: dict = Field(default_factory=dict)
    seed: int = Field(0,ge=0,le=2147483647)
    assemble_only: bool = False
    chapter_ids: list[str] = Field(default_factory=list,max_length=2000)
    segment_processing: list[dict] = Field(default_factory=list,max_length=10000)


class EffectsOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    chain: list[dict] = Field(default_factory=list, max_length=32)
    format: Literal['wav','flac','mp3','opus','m4b'] = 'wav'
    name: str = Field('processed', max_length=100)
    bypass: bool = False
    region: dict | None = None
    write_report: bool = True
    preview: bool = False
