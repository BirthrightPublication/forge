"""Local structured speech documents. Markup is parsed, never sent as spoken text.

Pacing inserts explicit silence targets; these are not claims about a neural
engine's natural delivery. Original manuscripts and source offsets are retained.
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import math
import re

MAX_TEXT = 1_000_000
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
EMOTION_NAMES = ('Neutral', 'Happy', 'Excited', 'Sad', 'Angry', 'Frustrated',
                 'Fearful', 'Nervous', 'Curious', 'Surprised', 'Disgusted', 'Calm',
                 'Peaceful', 'Serious', 'Confident', 'Suspicious', 'Sarcastic',
                 'Whispering', 'Dramatic', 'Mysterious', 'Comforting',
                 'Authoritative', 'Intimate', 'Energetic', 'Tired', 'Hesitant')
EMOTIONS = [{'id': name.lower(), 'name': name, 'base_emotion': name.lower(),
             'instructions': '' if name == 'Neutral' else f'Read with {name.lower()} delivery.',
             'method': 'engine-dependent', 'builtin': True,
             'description': 'Native expression requires a supporting engine/task; acoustic effects are labeled separately.'}
            for name in EMOTION_NAMES]
# These are opt-in acoustic interpretations, never native emotion tokens.
# All built-ins have a usable conservative DSP path, with honest labeling.
_ACOUSTIC = {'happy': (1, 1.05, 0), 'excited': (1.5, 1.12, 1), 'sad': (-1, .90, -2),
             'angry': (-.5, 1.08, 2), 'frustrated': (-.5, 1.04, 1), 'fearful': (1, 1.05, -1),
             'nervous': (.7, 1.08, -1), 'curious': (.5, 1, 0), 'surprised': (1.5, 1.05, 1),
             'disgusted': (-.5, .95, 0), 'calm': (-.3, .92, -1), 'peaceful': (-.5, .90, -2),
             'serious': (-.7, .96, 0), 'confident': (-.5, 1.02, 1), 'suspicious': (-.5, .94, -1),
             'sarcastic': (.3, .95, 0), 'whispering': (0, .95, -8), 'dramatic': (-1, .90, 1),
             'mysterious': (-1.5, .90, -3), 'comforting': (-.3, .92, -2),
             'authoritative': (-1, .96, 1), 'intimate': (-.5, .92, -5),
             'energetic': (.7, 1.12, 1), 'tired': (-1, .85, -3), 'hesitant': (0, .88, -2)}
for _preset in EMOTIONS:
    _pitch, _pace, _gain = _ACOUSTIC.get(_preset['id'], (0, 1, 0))
    _chain = []
    if _pitch or _pace != 1:
        _chain.append({'type': 'pitch', 'params': {'semitones': _pitch, 'tempo': _pace}})
    if _gain:
        _chain.append({'type': 'gain', 'params': {'db': _gain}})
    _preset['fallback'] = {'method': 'effects', 'chain': _chain,
                           'disclosure': 'Acoustic styling only; not native emotional speech or a simulated vocal event.'}

PAUSES = {'comma': 200, 'period': 425, 'semicolon': 325, 'paragraph': 750,
          'heading': 900, 'chapter': 1250, 'list': 450, 'quotation': 250,
          'ellipsis': 650, 'scene': 1500, 'line': 0}
_STYLES = [('normal', 'Normal Narration', {}), ('audiobook', 'Audiobook', {'paragraph': 850, 'chapter': 1500}),
           ('document', 'Document Reader', {'heading': 1050, 'list': 550}),
           ('dramatic', 'Dramatic Narration', {'period': 500, 'ellipsis': 800, 'scene': 2000}),
           ('news', 'News Reader', {'period': 350, 'paragraph': 600}),
           ('academic', 'Academic Reader', {'period': 500, 'list': 550}),
           ('poetry', 'Poetry Reader', {'line': 500, 'paragraph': 900}),
           ('dialogue', 'Dialogue Reader', {'quotation': 350}), ('custom', 'Custom', {})]
READING_STYLES = [{'id': key, 'name': name, 'pauses': {**PAUSES, **overrides},
                   'builtin': True, 'method': 'segmentation-and-inserted-silence'}
                  for key, name, overrides in _STYLES]


def identifier(value, label='identifier'):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f'Invalid {label}; use 1–128 letters, digits, dots, underscores, colons or hyphens.')
    return value


def _number(value):
    """Small English cardinal expansion; never used for unspecified languages."""
    small = ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen').split()
    tens = ('zero ten twenty thirty forty fifty sixty seventy eighty ninety').split()
    n = int(value)
    if n < 0: return 'minus ' + _number(-n)
    if n < 20: return small[n]
    if n < 100: return tens[n // 10] + (' ' + small[n % 10] if n % 10 else '')
    if n < 1000: return small[n // 100] + ' hundred' + (' ' + _number(n % 100) if n % 100 else '')
    for scale, name in ((1_000_000_000, 'billion'), (1_000_000, 'million'), (1000, 'thousand')):
        if n >= scale: return _number(n // scale) + ' ' + name + (' ' + _number(n % scale) if n % scale else '')
    return str(value)


def normalize_text(text, pronunciation=None, *, language='en', rules=None):
    """Return a spoken preview without mutating the source.

    Numeric/abbreviation rules are opt-in English rules. No cross-language
    guessing or phonetic alphabet conversion is performed here.
    """
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('Speech text must contain at most one million characters.')
    result = text
    entries = pronunciation or {}
    if isinstance(entries, dict):
        entries = [{'source': key, 'replacement': value} for key, value in entries.items()]
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError('Pronunciation dictionary must contain at most 1000 replacements.')
    checked = []
    for entry in entries:
        if not isinstance(entry, dict): raise ValueError('Invalid pronunciation entry.')
        source, replacement = entry.get('source', ''), entry.get('replacement', '')
        if not isinstance(source, str) or not source or len(source) > 200:
            raise ValueError('Pronunciation source must contain 1–200 characters.')
        if not isinstance(replacement, str) or not replacement or len(replacement) > 500 or '[[' in replacement or ']]' in replacement:
            raise ValueError('Pronunciation replacement must contain plain text, not script markers.')
        if entry.get('phonemes') or entry.get('phonetic'):
            raise ValueError('Phonetic overrides require an engine-specific pronunciation adapter; use a spoken replacement here.')
        checked.append((source, replacement))
    # A single pass prevents cascading replacements and treats entries as literal text.
    if checked:
        table = dict(checked)
        pattern = '|'.join(re.escape(key) for key in sorted(table, key=len, reverse=True))
        result = re.sub(pattern, lambda match: table[match.group()], result)
    rules = rules or {}
    if not isinstance(rules, dict): raise ValueError('Normalization rules must be an object.')
    allowed_rules = {'abbreviations', 'currency', 'decimals', 'acronyms', 'numbers',
                     'dates', 'times', 'measurements', 'urls', 'email'}
    if set(rules) - allowed_rules or any(not isinstance(enabled, bool) for enabled in rules.values()):
        raise ValueError('Unknown normalization rule or nonboolean rule setting.')
    if language.split('-')[0].lower() == 'en':
        if rules.get('abbreviations'):
            for source, target in {'Dr.': 'Doctor', 'Mr.': 'Mister', 'Mrs.': 'Missus', 'Prof.': 'Professor', 'e.g.': 'for example', 'i.e.': 'that is'}.items():
                result = re.sub(r'(?<!\w)' + re.escape(source), lambda _: target, result)
        if rules.get('currency'):
            def currency(match):
                sign, whole, cents = match.group(1), match.group(2).replace(',', ''), match.group(3)
                unit = {'$': 'dollar', '£': 'pound', '€': 'euro'}[sign]
                n = int(whole)
                value = _number(n) + ' ' + unit + ('s' if n != 1 else '')
                if cents and int(cents):
                    minor = ('penny' if int(cents) == 1 else 'pence') if sign == '£' else ('cent' if int(cents) == 1 else 'cents')
                    value += ' and ' + _number(int(cents)) + ' ' + minor
                return value
            result = re.sub(r'([$£€])(\d{1,3}(?:,\d{3})+|\d{1,9})(?:\.(\d{2}))?(?!\d)', currency, result)
        if rules.get('dates'):
            def date(match):
                try: value = datetime.date.fromisoformat(match[0])
                except ValueError: return match[0]
                return f'{calendar.month_name[value.month]} {_number(value.day)}, {_number(value.year)}'
            # Unambiguous ISO dates only. Locale-dependent dates are left for
            # a user pronunciation dictionary rather than silently guessed.
            result = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', date, result)
        if rules.get('times'):
            def clock(match):
                hours, minutes = int(match[1]), int(match[2])
                if hours > 23 or minutes > 59: return match[0]
                return _number(hours) + (' hundred' if not minutes else (' oh ' if minutes < 10 else ' ') + _number(minutes))
            result = re.sub(r'(?<![\w:])(\d{1,2}):(\d{2})(?!\w|:\d)', clock, result)
        if rules.get('decimals'):
            result = re.sub(r'(?<![\w.])(\d{1,9})\.(\d+)(?!\w|\.\d)', lambda m: _number(int(m[1])) + ' point ' + ' '.join(_number(int(n)) for n in m[2]), result)
        if rules.get('measurements'):
            units = {'kg': ('kilogram', 'kilograms'), 'g': ('gram', 'grams'), 'km': ('kilometer', 'kilometers'),
                     'cm': ('centimeter', 'centimeters'), 'mm': ('millimeter', 'millimeters'),
                     'm': ('meter', 'meters'), 'ml': ('milliliter', 'milliliters'), 'L': ('liter', 'liters')}
            result = re.sub(r'(?<![\w.])(\d{1,9})\s*(kg|km|cm|mm|ml|g|m|L)\b',
                            lambda m: _number(int(m[1])) + ' ' + units[m[2]][int(m[1]) != 1], result)
        if rules.get('email'):
            result = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
                            lambda m: m[0].replace('@', ' at ').replace('.', ' dot ').replace('_', ' underscore '), result)
        if rules.get('urls'):
            result = re.sub(r'https?://[^\s<>\[\]]+', lambda m: m[0].replace('https://', '').replace('http://', '').replace('/', ' slash ').replace('.', ' dot ').rstrip(), result)
        if rules.get('acronyms'):
            result = re.sub(r'\b[A-Z]{2,6}\b', lambda m: ' '.join(m[0]), result)
        if rules.get('numbers'):
            result = re.sub(r'(?<![\w./:-])\d{1,9}(?![\w./:-])', lambda m: _number(int(m[0])), result)
    return result


def detect_structure(text):
    result, offset = [], 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped: kind = 'paragraph-break'
        elif re.fullmatch(r'(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,}', stripped): kind = 'scene'
        elif re.match(r'(?i)chapter\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five)\b', stripped): kind = 'chapter'
        elif re.match(r'#{1,6}\s', stripped): kind = 'heading'
        elif re.match(r'(?:[-+*]|\d+[.)])\s', stripped): kind = 'list'
        elif stripped.startswith(('>', '"', '“')): kind = 'quotation'
        elif '|' in stripped and stripped.count('|') >= 2: kind = 'table'
        elif re.match(r'\[\^?\d+\]:', stripped): kind = 'footnote'
        else: kind = 'paragraph'
        result.append({'kind': kind, 'start': offset, 'end': offset + len(line), 'text': line.rstrip('\r\n'),
                       'level': len(line) - len(line.lstrip()) if kind == 'list' else len(line) - len(line.lstrip('#')) if kind == 'heading' else 0})
        offset += len(line)
    return result


def _style(style, overrides):
    if isinstance(style, dict):
        value = {**PAUSES, **style.get('pauses', {})}
        key = identifier(style.get('id', 'custom'), 'style')
    else:
        key = identifier(style, 'style')
        known = next((item for item in READING_STYLES if item['id'] == key), None)
        if not known: raise ValueError('Unknown reading style; pass the saved style object.')
        value = dict(known['pauses'])
    if overrides:
        if not isinstance(overrides, dict) or set(overrides) - set(PAUSES): raise ValueError('Unknown pacing rule.')
        value.update(overrides)
    for name, duration in value.items():
        if name not in PAUSES or isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration <= 30_000:
            raise ValueError('Pause targets must be finite milliseconds from 0 to 30000.')
        value[name] = int(duration)
    return key, value


def parse_script(text, *, speaker='narrator', emotion='neutral', style='normal', pronunciation=None, pauses=None,
                 language='en', normalization=None):
    """Parse stable editor markup into ordered speech/silence segments.

    IDs depend on semantic content and duplicate occurrence, not byte positions;
    changing one sentence leaves other sentence IDs suitable for render reuse.
    """
    if not isinstance(text, str) or len(text) > MAX_TEXT or '\x00' in text:
        raise ValueError('Speech text must contain at most one million characters and no NUL.')
    speaker = identifier(speaker, 'speaker')
    emotion = identifier(emotion, 'emotion')
    style_id, pacing = _style(style, pauses)
    current = {'speaker': speaker, 'emotion': emotion, 'style': style_id}
    emotion_stack, style_stack, result = [], [], []

    def silence(ms, origin, start, end):
        if not ms: return
        if result and result[-1]['kind'] == 'silence':
            last = result[-1]
            if origin == 'manual':
                last['pause_ms'] = last['pause_ms'] + ms if last['method'] == 'manual' else ms
                last['method'] = 'manual'
            elif last['method'] != 'manual': last['pause_ms'] = max(last['pause_ms'], ms)
            if last['pause_ms'] > 60_000: raise ValueError('Adjacent pauses exceed one minute.')
            last['end'] = end
            return
        result.append({'kind': 'silence', 'pause_ms': ms, 'method': origin, 'text': '', 'original_text': '', 'start': start, 'end': end, **current})

    def add_text(raw, offset):
        # Split sentence/pacing punctuation without breaking decimal values or
        # common abbreviations. Text structure remains a user-editable heuristic.
        position = 0
        for match in re.finditer(r'\n[ \t]*\n+|\n|\.\.\.|…|[,;:]|[.!?](?:[”"\']?)(?=\s|$)', raw):
            boundary = match.group()
            prefix = raw[position:match.end()]
            if boundary.startswith('.') and (re.search(r'\b(?:Mr|Mrs|Dr|Prof|St|vs|etc)\.$', prefix, re.I) or re.search(r'\b[A-Z]\.$', prefix)):
                continue
            emit_text(prefix, offset + position)
            if boundary.startswith('\n'):
                kind = 'paragraph' if boundary.count('\n') >= 2 else 'line'
            else: kind = 'ellipsis' if boundary in ('...', '…') else 'comma' if boundary == ',' else 'semicolon' if boundary in (';', ':') else 'period'
            silence(pacing[kind], 'structure', offset + match.start(), offset + match.end())
            position = match.end()
        emit_text(raw[position:], offset + position)

    def emit_text(raw, offset):
        original = raw.strip()
        if not original: return
        structure = detect_structure(raw)
        kind = structure[0]['kind'] if structure else 'paragraph'
        if kind == 'scene':
            silence(pacing['scene'], 'structure', offset, offset + len(raw)); return
        spoken = re.sub(r'^\s*#{1,6}\s+', '', original)
        spoken = re.sub(r'^\s*[-+*]\s+', '', spoken)
        spoken = normalize_text(spoken, pronunciation, language=language, rules=normalization)
        start = offset + len(raw) - len(raw.lstrip())
        result.append({'kind': 'speech', 'text': spoken, 'original_text': original, 'start': start,
                       'end': offset + len(raw.rstrip()), 'structure': kind, **current})
        if kind in ('heading', 'chapter', 'list'):
            silence(pacing[kind], 'structure', offset, offset + len(raw))

    position = 0
    for marker in re.finditer(r'\[\[([^\[\]]*)\]\]', text):
        chunk = text[position:marker.start()]
        if '[[' in chunk or ']]' in chunk: raise ValueError('Malformed speech marker.')
        add_text(chunk, position)
        value = marker[1]
        if value.startswith('pause:'):
            match = re.fullmatch(r'pause:(\d{1,5})ms', value)
            if not match or not 0 <= int(match[1]) <= 30_000: raise ValueError('Manual pauses must be 0–30000 ms.')
            silence(int(match[1]), 'manual', marker.start(), marker.end())
        elif value.startswith('speaker:'):
            current['speaker'] = identifier(value[8:], 'speaker')
        elif value.startswith('emotion:'):
            if len(emotion_stack) >= 8: raise ValueError('Emotion marker nesting is too deep.')
            emotion_stack.append(current['emotion']); current['emotion'] = identifier(value[8:], 'emotion')
        elif value == '/emotion':
            if not emotion_stack: raise ValueError('An emotion closing marker has no opening marker.')
            current['emotion'] = emotion_stack.pop()
        elif value.startswith('style:'):
            if len(style_stack) >= 8: raise ValueError('Style marker nesting is too deep.')
            style_stack.append((current['style'], pacing))
            current['style'], pacing = _style(value[6:], None)
        elif value == '/style':
            if not style_stack: raise ValueError('A style closing marker has no opening marker.')
            current['style'], pacing = style_stack.pop()
        elif value.startswith('event:'):
            # Vocal events require adapter capability checks; keep separate from speech.
            event = identifier(value[6:], 'vocal event')
            result.append({'kind': 'event', 'event': event, 'text': '', 'original_text': '',
                           'start': marker.start(), 'end': marker.end(), **current})
        else:
            raise ValueError('Unsupported speech marker: ' + value[:100])
        position = marker.end()
    chunk = text[position:]
    if '[[' in chunk or ']]' in chunk: raise ValueError('Malformed speech marker.')
    add_text(chunk, position)
    if emotion_stack or style_stack: raise ValueError('Close emotion and style markers before generating.')
    occurrences = {}
    for segment in result:
        identity = {key: segment[key] for key in ('kind', 'text', 'speaker', 'emotion', 'style')}
        identity.update({key: segment[key] for key in ('pause_ms', 'method', 'event') if key in segment})
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        occurrence = occurrences.get(digest, 0); occurrences[digest] = occurrence + 1
        segment['id'] = hashlib.sha256(f'{digest}:{occurrence}'.encode()).hexdigest()[:32]
    return result


def compile_document(text, **options):
    segments = parse_script(text, **options)
    spoken = ' '.join(item['text'] for item in segments if item['kind'] == 'speech')
    words = len(re.findall(r"\b[\w]+(?:['’-][\w]+)*\b", spoken))
    return {'source': text, 'spoken_text': spoken, 'segments': segments, 'characters': len(text), 'words': words,
            'estimated_seconds': round(words / 150 * 60 + sum(item.get('pause_ms', 0) for item in segments) / 1000, 2),
            'estimate_method': '150 words per minute plus configured silence; actual synthesis duration may differ',
            'structure': detect_structure(text)}


def insert_marker(text, start, end, kind, value):
    """Editor helper: cursor insertion, selection wrapping, or explicit speaker marker."""
    if not isinstance(text, str) or isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(text):
        raise ValueError('Invalid text selection.')
    if kind == 'pause':
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30_000: raise ValueError('Pause must be 0–30000 ms.')
        replacement = f'[[pause:{value}ms]]' + text[start:end]
    elif kind in ('emotion', 'style'):
        value = identifier(value, kind)
        replacement = f'[[{kind}:{value}]]' + text[start:end] + f'[[/{kind}]]'
    elif kind in ('speaker', 'event'):
        replacement = f'[[{kind}:{identifier(value, kind)}]]' + text[start:end]
    else: raise ValueError('Unsupported editor marker.')
    result = text[:start] + replacement + text[end:]
    if len(result) > MAX_TEXT: raise ValueError('Speech text exceeds one million characters.')
    return result
