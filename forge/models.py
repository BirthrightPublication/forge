from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    timestamps: Literal['none','segment','sentence','word'] = 'segment'
    formats: list[Literal['txt','srt','vtt','json','csv','html']] = Field(default_factory=lambda: ['txt','srt','json','html'], min_length=1, max_length=6)
    beam_size: int = Field(5, ge=1, le=10)
    vad: bool = True
    vocabulary: str = Field('', max_length=2000)
    allow_model_download: bool = False
    style: Literal['raw','clean'] = 'raw'
    remove_fillers: bool = True
    subtitle_chars: int = Field(42, ge=20, le=80)
    subtitle_lines: int = Field(2, ge=1, le=3)
    subtitle_seconds: float = Field(6, ge=1, le=15)
    flag_uncertain: bool = True
    diarization: bool = False
    diarization_model: str = Field('', max_length=2000)
    min_speakers: int = Field(1, ge=1, le=20)
    max_speakers: int = Field(6, ge=1, le=20)

    @model_validator(mode='after')
    def speaker_bounds(self):
        if self.min_speakers > self.max_speakers:
            raise ValueError('Minimum speakers cannot exceed maximum speakers.')
        if self.diarization and not self.diarization_model:
            raise ValueError('Select a local Community-1 model folder for speaker labeling.')
        return self

class Retune(Strict):
    source_hz: float = Field(440, ge=400, le=480)
    target_hz: float = Field(432, ge=400, le=480)
    preserve_tempo: bool = True
    format: Literal['mp3','flac','wav','m4a','mp4','mkv'] = 'mp3'
    keep_tags: bool = True
    quality: Literal['high','standard'] = 'high'
    normalize: bool = False
    loudness_lufs: float = Field(-16, ge=-30, le=-9)
    true_peak_db: float = Field(-1.5, ge=-9, le=-1)
    mp3_bitrate: Literal[0,128,192,256,320] = 0
    wav_bits: Literal[16,24] = 24

class Metadata(Strict):
    audit_report: bool = False
    mode: Literal['wipe','selective','edit'] = 'wipe'
    remove_tags: list[str] = Field(default_factory=list, max_length=40)
    tags: dict[str,str] = Field(default_factory=dict, max_length=20)

    @field_validator('remove_tags')
    @classmethod
    def valid_removals(cls, value):
        allowed = {'title','artist','album','album_artist','comment','description','date','genre','track','disc','composer','copyright','location','creation_time','encoder'}
        if any(t not in allowed for t in value):
            raise ValueError('Select only the supported metadata fields.')
        return list(dict.fromkeys(value))

    @field_validator('tags')
    @classmethod
    def valid_tags(cls, value):
        allowed = {'title','artist','album','album_artist','comment','description','date','genre','track','disc','composer','copyright'}
        if any(k not in allowed or len(v)>4000 or '\x00' in v for k,v in value.items()):
            raise ValueError('Unsupported metadata key or oversized value.')
        return value

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
    playlist: bool = False
    playlist_start: int = Field(1, ge=1, le=10000)
    playlist_end: int = Field(25, ge=1, le=10000)
    subtitles: bool = False
    auto_subtitles: bool = False
    subtitle_languages: str = Field('en', max_length=120, pattern=r'^[a-zA-Z0-9,.*_-]+$')
    filename_style: Literal['title_id','channel_title','playlist_index'] = 'title_id'

    @model_validator(mode='after')
    def playlist_bounds(self):
        if self.playlist and (self.playlist_end < self.playlist_start or self.playlist_end-self.playlist_start >= 500):
            raise ValueError('Playlist range must be ordered and limited to 500 entries.')
        return self

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
    workers: int = Field(2, ge=1, le=8)
    cpu_workers: int = Field(2, ge=1, le=8)
    gpu_workers: int = Field(1, ge=1, le=2)
    download_workers: int = Field(2, ge=1, le=4)
    desktop_notifications: bool = False
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


class JobOrder(Strict):
    direction: Literal['up','down','first','last']

class BatchAction(Strict):
    ids: list[str] = Field(min_length=1, max_length=10000)
    action: Literal['cancel','retry']

class WorkflowStep(Strict):
    tool: Literal['retune','metadata','transcribe']
    options: dict = Field(default_factory=dict)

class Workflow(Strict):
    name: str = Field('Production preparation', min_length=1, max_length=80)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=6)
    output: Output = Field(default_factory=Output)

    @model_validator(mode='after')
    def validate_steps(self):
        for index, step in enumerate(self.steps):
            step.options = OPTION_MODELS[step.tool].model_validate(step.options).model_dump()
            if step.tool == 'transcribe' and index != len(self.steps)-1:
                raise ValueError('Transcription must be the last workflow step: its outputs are text, not media.')
            if step.tool == 'metadata' and step.options['mode'] != 'wipe':
                raise ValueError('Workflow metadata steps currently use full supported-field reduction.')
        return self

class WorkflowRun(Workflow):
    sources: list[str] = Field(min_length=1, max_length=200)

class ModelDownload(Strict):
    model: Literal['tiny','base','small','medium','large-v3','large-v3-turbo']
    consent: bool = False
