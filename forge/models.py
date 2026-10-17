from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Output(Strict):
    mode: Literal['source', 'custom', 'last', 'default'] = 'source'
    directory: str = Field('', max_length=2000)
    subfolder: str = Field('', max_length=80)

class Transcribe(Strict):
    model: Literal['tiny','base','small','medium','large-v3','large-v3-turbo'] = 'large-v3'
    device: Literal['auto','cpu','cuda'] = 'auto'
    language: str = Field('', max_length=8, pattern=r'^([a-z]{2,3})?$')
    task: Literal['transcribe','translate'] = 'transcribe'
    timestamps: Literal['none','segment','word'] = 'segment'
    formats: list[Literal['txt','srt','vtt','json']] = Field(default_factory=lambda: ['txt','srt','json'], min_length=1, max_length=4)
    beam_size: int = Field(5, ge=1, le=10)
    vad: bool = True
    vocabulary: str = Field('', max_length=2000)
    allow_model_download: bool = False

class Retune(Strict):
    source_hz: float = Field(440, ge=400, le=480)
    target_hz: float = Field(432, ge=400, le=480)
    preserve_tempo: bool = True
    format: Literal['mp3','flac','wav','m4a','mp4','mkv'] = 'mp3'
    keep_tags: bool = True
    quality: Literal['high','standard'] = 'high'

class Metadata(Strict):
    audit_report: bool = False

class QR(Strict):
    text: str = Field(min_length=1, max_length=2000)
    name: str = Field('QR Code', min_length=1, max_length=100)
    format: Literal['png','svg'] = 'png'
    error_correction: Literal['L','M','Q','H'] = 'M'
    box_size: int = Field(12, ge=4, le=30)
    border: int = Field(4, ge=4, le=16)

class Download(Strict):
    format: Literal['mp3','m4a','flac','wav','mp4','mkv'] = 'mp3'
    quality: Literal['best','320','192','128'] = 'best'
    max_height: Literal[0,720,1080,1440,2160] = 0
    embed_metadata: bool = True
    cookies: str = Field('', max_length=2000)

class Password(Strict):
    length: int = Field(24, ge=8, le=256)
    count: int = Field(1, ge=1, le=50)
    upper: bool = True
    numbers: bool = True
    symbols: str = Field('!@#$%^-_=+[]{}?', max_length=128)
    unicode_chars: bool = False
    exclude_ambiguous: bool = True

class JobRequest(Strict):
    tool: Literal['transcribe','retune','metadata','qr','download_audio','download_video']
    sources: list[str] = Field(default_factory=list, max_length=500)
    urls: list[str] = Field(default_factory=list, max_length=100)
    options: dict = Field(default_factory=dict)
    output: Output = Field(default_factory=Output)

class Register(Strict):
    paths: list[str] = Field(min_length=1, max_length=500)

class Pick(Strict):
    kind: Literal['files','folder','executable'] = 'files'

class Settings(Strict):
    workers: int = Field(2, ge=1, le=3)
    ffmpeg: str = Field('', max_length=2000)
    ffprobe: str = Field('', max_length=2000)
    default_output: str = Field('', max_length=2000)
    last_output: str = Field('', max_length=2000)
    daily_update_check: bool = False
    queue_paused: bool = False

class QueueAction(Strict):
    paused: bool

class Preset(Strict):
    tool: str = Field(max_length=40)
    name: str = Field(min_length=1, max_length=60)
    options: dict
    output: Output

OPTION_MODELS = {'transcribe': Transcribe, 'retune': Retune, 'metadata': Metadata,
                 'qr': QR, 'download_audio': Download, 'download_video': Download}
