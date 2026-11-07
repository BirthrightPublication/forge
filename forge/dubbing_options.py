"""Bounded local dubbing options shared by APIs and linear queue stages."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, validate_default=True)


class RecognitionOptions(Strict):
    engine: Literal['whisper'] = 'whisper'
    model: Literal['tiny','base','small','medium','large-v3','large-v3-turbo'] = 'large-v3-turbo'
    device: Literal['auto','cpu','cuda'] = 'auto'
    language: Literal['en'] = 'en'
    model_revision: str = Field('', pattern=r'^(|[0-9a-f]{40})$')
    beam_size: int = Field(5, ge=1, le=10)
    vad: bool = True
    diarization: bool = False
    diarization_model: str = Field('', max_length=2000)
    min_speakers: int = Field(1, ge=1, le=20)
    max_speakers: int = Field(6, ge=1, le=20)
    vocabulary: str = Field('', max_length=2000)


class TranslationOptions(Strict):
    engine: str = Field('opus-en-ru', min_length=1, max_length=100)
    model_revision: str = Field('', max_length=100)
    device: Literal['auto','cpu','cuda'] = 'cpu'
    preserve_numbers: bool = True
    preserve_names: bool = True
    glossary_id: str = Field('', max_length=80)


class RussianVoiceOptions(Strict):
    engine: str = Field('qwen3-tts-0.6b-base', min_length=1, max_length=100)
    model_revision: str = Field('', max_length=100)
    device: Literal['auto','cpu','cuda'] = 'auto'
    voice_profile_id: str = Field('', max_length=80)
    language: Literal['ru','Russian'] = 'ru'
    speed: float = Field(1, ge=.75, le=1.25)
    pitch: float = Field(0, ge=-6, le=6)
    expression: str = Field('', max_length=100)
    effects: list[dict] = Field(default_factory=list, max_length=32)
    x_vector_only_mode: bool = False


class TimingOptions(Strict):
    strategy: Literal['preserve','natural','balanced','manual'] = 'balanced'
    max_tempo: float = Field(1.15, ge=1, le=1.25)
    max_slowdown: float = Field(.85, ge=.75, le=1)
    gap_seconds: float = Field(.08, ge=0, le=5)
    allow_overlap: bool = False


class AudioOptions(Strict):
    original_voice_db: float = Field(-18, ge=-96, le=12)
    background_db: float = Field(0, ge=-96, le=12)
    narration_db: float = Field(0, ge=-96, le=12)
    music_db: float = Field(0, ge=-96, le=12)
    ducking: bool = True
    ducking_db: float = Field(-12, ge=-36, le=0)
    separation: Literal['off','manual','hdemucs'] = 'off'
    background_artifact_id: str = Field('', max_length=100)
    music_artifact_id: str = Field('', max_length=100)
    vocals_artifact_id: str = Field('', max_length=100)
    separation_device: Literal['auto','cpu','cuda'] = 'auto'
    separation_chunk_seconds: float = Field(10, ge=4, le=20)
    loudness_lufs: float = Field(-16, ge=-30, le=-9)
    true_peak_db: float = Field(-1.5, ge=-9, le=-1)
    compression: bool = True
    limiter: bool = True
    effects: list[dict] = Field(default_factory=list, max_length=32)


class LipRegion(Strict):
    id: str = Field('', max_length=80)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    box: list[float] = Field(default_factory=list, max_length=4)
    face_id: str = Field('', max_length=80)
    normalized: bool = False

    @model_validator(mode='after')
    def bounds(self):
        if self.end <= self.start or self.end > 48 * 3600:
            raise ValueError('Choose a positive lip-sync region within the source duration.')
        if self.box and (len(self.box) != 4 or any(not math.isfinite(x) or x < 0 for x in self.box)):
            raise ValueError('A selected face needs four finite nonnegative crop coordinates.')
        return self


class FaceSelection(Strict):
    id: str = Field(min_length=1, max_length=80)
    box: list[float] = Field(min_length=4, max_length=4)
    normalized: bool = False
    consent: bool = False

    @model_validator(mode='after')
    def bounded_box(self):
        bound=1 if self.normalized else 32768
        if any(not math.isfinite(value) or not 0<=value<=bound for value in self.box) or self.box[2]<=0 or self.box[3]<=0:
            raise ValueError('Select a finite, positive face crop inside the video.')
        return self


class SpeechWindow(Strict):
    start: float = Field(ge=0, le=48*3600)
    end: float = Field(gt=0, le=48*3600)

    @model_validator(mode='after')
    def duration(self):
        if self.end<=self.start: raise ValueError('Choose a positive speech window.')
        return self


class LipOptions(Strict):
    mode: Literal['off','automatic','selected','full'] = 'off'
    engine: str = Field('musetalk-1.5', max_length=100)
    model_revision: str = Field('', max_length=100)
    device: Literal['auto','cpu','cuda'] = 'auto'
    consent: bool = False
    regions: list[LipRegion] = Field(default_factory=list, max_length=500)
    preview_seconds: float = Field(5, ge=5, le=10)
    preview_start: float = Field(0, ge=0)
    batch_size: int = Field(1, ge=1, le=4)
    max_resolution: int = Field(1080, ge=256, le=2160)
    face_id: str = Field('', max_length=80)
    faces: list[FaceSelection] = Field(default_factory=list, max_length=20)
    speech_windows: list[SpeechWindow] = Field(default_factory=list, max_length=10000)


class ExportOptions(Strict):
    directory: str = Field('', max_length=2000)
    container: Literal['mp4','mkv'] = 'mp4'
    video_codec: Literal['copy','libx264','libx265','h264_nvenc','hevc_nvenc','av1_nvenc'] = 'copy'
    audio_codec: Literal['aac','flac','pcm_s24le','libopus'] = 'aac'
    quality: Literal['high','standard','compact'] = 'high'
    bitrate: int = Field(0, ge=0, le=200_000_000)
    crf: int | None = Field(None, ge=12, le=35)
    preserve_resolution: bool = True
    preserve_frame_rate: bool = True
    resolution: Literal['source','480p','720p','1080p','1440p','2160p'] = 'source'
    frame_rate: float | Literal['source'] = 'source'
    subtitles: Literal['sidecar','embed','burn','none'] = 'sidecar'
    separate_audio: list[Literal['wav','flac']] = Field(default_factory=lambda:['wav'], max_length=2)
    bilingual: bool = False
    project_backup: bool = False
    provenance: bool = False

    @field_validator('frame_rate')
    @classmethod
    def supported_fps(cls,value):
        if value!='source' and (not math.isfinite(value) or not 1<=value<=120):
            raise ValueError('Choose a finite export frame rate between 1 and 120, or preserve the source.')
        return value


class PublishingOptions(Strict):
    profile_id: str = Field('', max_length=80)
    title: str = Field('', max_length=100)
    description: str = Field('', max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    visibility: Literal['private','unlisted','public'] = 'private'
    schedule: str = Field('', max_length=80)
    timezone: str = Field('Europe/Moscow', max_length=100)
    made_for_kids: bool | None = None
    contains_synthetic_media: bool = True
    english_video_url: str = Field('', max_length=2000)


class DubbingOptions(Strict):
    asr: RecognitionOptions = Field(default_factory=RecognitionOptions)
    translation: TranslationOptions = Field(default_factory=TranslationOptions)
    tts: RussianVoiceOptions = Field(default_factory=RussianVoiceOptions)
    timing: TimingOptions = Field(default_factory=TimingOptions)
    audio: AudioOptions = Field(default_factory=AudioOptions)
    lip_sync: LipOptions = Field(default_factory=LipOptions)
    export: ExportOptions = Field(default_factory=ExportOptions)
    publishing: PublishingOptions = Field(default_factory=PublishingOptions)
    panels: dict = Field(default_factory=dict)

    @field_validator('panels')
    @classmethod
    def panel_preferences(cls, value):
        allowed={'left_width','right_width','timeline_height','left_collapsed','right_collapsed','timeline_collapsed','active_tab'}
        if set(value)-allowed:
            raise ValueError('Unsupported dubbing panel preference.')
        for key,item in value.items():
            if key.endswith('_collapsed') and type(item) is not bool:
                raise ValueError('Panel visibility must be a boolean.')
            if key.endswith(('_width','_height')) and (type(item) not in (int,float) or not math.isfinite(item) or not 100 <= item <= 1600):
                raise ValueError('Panel dimensions must be between 100 and 1,600 pixels.')
            if key=='active_tab' and item not in {'script','timeline','setup','voices','export','publishing'}:
                raise ValueError('Unsupported dubbing panel.')
        return value


STAGE_TITLES = {
    'dubbing_transcribe':'Transcribe Source', 'dubbing_translate':'Translate Text',
    'dubbing_review':'Review Translation Gate', 'dubbing_voice':'Generate Russian Voice',
    'dubbing_voice_reference':'Prepare Voice Reference', 'dubbing_align':'Synchronize Narration',
    'dubbing_separate':'Separate Audio', 'dubbing_mix':'Mix Audio',
    'dubbing_effects':'Apply Audio Effects',
    'dubbing_lipsync':'Lip Sync', 'dubbing_encode':'Encode Video',
    'dubbing_export':'Export Localized Media', 'dubbing_metadata':'Prepare YouTube Metadata',
    'dubbing_prepare_upload':'Prepare Upload', 'dubbing_install':'Install Dubbing Component',
    'dubbing_model_test':'Test Dubbing Component',
}
PIPELINE_STAGES = ('dubbing_transcribe','dubbing_translate','dubbing_review','dubbing_voice',
                   'dubbing_align','dubbing_mix','dubbing_encode','dubbing_metadata',
                   'dubbing_export','dubbing_prepare_upload')


class DubbingJobOptions(Strict):
    project_id: str = Field('', max_length=80, pattern=r'^[A-Za-z0-9_-]*$')
    run_id: str = Field('', max_length=80, pattern=r'^[A-Za-z0-9_-]*$')
    stage: str = Field('', max_length=100)
    name: str = Field('', max_length=200)
    segment_ids: list[str] | None = Field(None, max_length=10000)
    preview: bool = False
    preview_seconds: float = Field(5, ge=.1, le=30)
    preview_start: float = Field(0, ge=0)
    consent: bool = False
    component: str = Field('', max_length=100)
    components: list[str] = Field(default_factory=list, max_length=10)
    device: Literal['cpu','cuda','auto'] = 'auto'
    action: Literal['install','verify','repair','test','move','remove_unused','compatibility'] = 'install'
    plan_token: str = Field('', max_length=64)
    destination: str = Field('', max_length=2000)
    replace_manual_text: bool = False
    voice_profile_id: str = Field('', max_length=80)
    reference_text: str = Field('', max_length=10000)
    description: str = Field('', max_length=2000)
    engine: str = Field('', max_length=100)
    effects: list[dict] = Field(default_factory=list, max_length=32)
    reference_start: float = Field(0, ge=0)
    reference_end: float = Field(0, ge=0)
    formats: list[Literal['video','wav','flac','srt','vtt','txt','metadata','bilingual','project','provenance']] | None = Field(None, min_length=1, max_length=10)
    include_source: bool = True

    @field_validator('segment_ids')
    @classmethod
    def unique_segments(cls, value):
        if value is not None and (len(value)!=len(set(value)) or any(not isinstance(x,str) or not 1 <= len(x) <= 80 for x in value)):
            raise ValueError('Select unique saved dubbing segment identities.')
        return value
