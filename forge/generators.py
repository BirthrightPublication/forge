"""Bounded, offline generators. Values are returned to the caller, never persisted.

Names and addresses are assembled from small fictional/test-data vocabularies.
Region labels describe formatting inspiration, not nationality or identity records.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import string
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


GeneratorID = Literal['identity', 'obfuscation', 'addresses', 'strings']
AMBIGUOUS = 'Il1O0o'
RESERVED_DOMAINS = ('example.com', 'example.org', 'example.net', 'example.test')
NOTICE = 'Synthetic test data / fictional privacy alias. Not an official identity or address record.'


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    count: int = Field(10, ge=1, le=1000)


class IdentityConfig(Config):
    kind: Literal['legal', 'pseudonym', 'normie'] = 'legal'
    region: Literal['international', 'anglophone', 'hispanic', 'japanese'] = 'international'
    convention: Literal['given_family', 'family_given', 'initial_family'] = 'given_family'
    separator: Literal[' ', '_', '-', '.'] = ' '
    suffix_digits: int = Field(0, ge=0, le=6)


class CharacterConfig(Config):
    length: int = Field(24, ge=1, le=256)
    lower: bool = True
    upper: bool = True
    numbers: bool = True
    symbols: str = Field('!@#$%^-_=+?', max_length=128)
    exclude: str = Field('', max_length=128)
    exclude_ambiguous: bool = True

    @field_validator('symbols', 'exclude')
    @classmethod
    def printable(cls, value):
        if any(not c.isprintable() or c.isspace() for c in value):
            raise ValueError('Character policies accept printable characters without whitespace.')
        return ''.join(dict.fromkeys(value))


class StringsConfig(CharacterConfig):
    kind: Literal['password', 'username', 'localpart', 'email', 'token', 'identifier'] = 'password'
    alphabet: Literal['policy', 'hex', 'base64url', 'custom'] = 'policy'
    custom_chars: str = Field('', max_length=128)
    domain: Literal['example.com', 'example.org', 'example.net', 'example.test'] = 'example.com'

    @field_validator('custom_chars')
    @classmethod
    def printable_custom(cls, value):
        return cls.printable(value)

    @model_validator(mode='after')
    def character_policy(self):
        if self.kind == 'password' and self.length < 8:
            raise ValueError('Passwords require at least eight characters.')
        if self.kind in {'username', 'localpart', 'email'} and self.length > 64:
            raise ValueError('Usernames and email local parts are limited to 64 characters.')
        groups = character_groups(self)
        if self.length < len(groups):
            raise ValueError('Length must allow at least one character from every selected group.')
        return self


class ObfuscationConfig(CharacterConfig):
    kind: Literal['plate', 'pattern', 'identifier', 'username', 'password'] = 'plate'
    pattern: str = Field('AAA-9999', min_length=1, max_length=128)

    @model_validator(mode='after')
    def identifier_policy(self):
        if self.kind in {'plate', 'pattern'}:
            if any(c not in 'Aa9X-_. ' for c in self.pattern) or not any(c in 'Aa9X' for c in self.pattern):
                raise ValueError('Patterns use A (uppercase), a (lowercase), 9 (digit), X (alphanumeric), and - _ . space separators.')
            excluded = self.exclude + (AMBIGUOUS if self.exclude_ambiguous else '')
            for c in self.pattern:
                if c in PATTERN_ALPHABETS and not available(PATTERN_ALPHABETS[c], excluded):
                    raise ValueError('Exclusions remove all characters from a pattern group.')
                if c not in PATTERN_ALPHABETS and c in excluded:
                    raise ValueError('A literal pattern separator is also excluded.')
        else:
            StringsConfig(**self.string_options())
        return self

    def string_options(self):
        return {k: v for k, v in self.model_dump().items() if k != 'pattern'}


ADDRESS_FIELDS = ('street', 'unit', 'city', 'region', 'postal_code', 'country')


class AddressConfig(Config):
    country: Literal['US', 'GB', 'CA', 'DE', 'AU', 'JP'] = 'US'
    include_unit: bool = False
    fields: list[Literal['street', 'unit', 'city', 'region', 'postal_code', 'country']] = Field(
        default_factory=lambda: list(ADDRESS_FIELDS), min_length=1, max_length=6)

    @field_validator('fields')
    @classmethod
    def distinct(cls, value):
        return list(dict.fromkeys(value))


MODELS = {'identity': IdentityConfig, 'obfuscation': ObfuscationConfig, 'addresses': AddressConfig, 'strings': StringsConfig}
TOOL_INFO = {
    'identity': {'title': 'Identity Studio', 'description': 'Synthetic names, ordinary aliases and online pseudonyms.'},
    'obfuscation': {'title': 'Obfuscation Studio', 'description': 'Fictional plate-like patterns, identifiers and secure strings.'},
    'addresses': {'title': 'Random Address Generator', 'description': 'Clearly synthetic addresses with country-inspired formatting.'},
    'strings': {'title': 'Random String Generator', 'description': 'Secure passwords, tokens, usernames and reserved-domain emails.'},
}

NAMES = {
    'international': (('Ari', 'Mira', 'Nora', 'Luca', 'Sam', 'Remy', 'Noel', 'Alex'), ('Vale', 'Rowan', 'Marlow', 'Ellery', 'Finch', 'Alden', 'Linden', 'Hale')),
    'anglophone': (('Alex', 'Jamie', 'Morgan', 'Taylor', 'Robin', 'Jordan', 'Casey', 'Riley'), ('Hayes', 'Brooks', 'Reed', 'Lane', 'Parker', 'Bell', 'Mills', 'West')),
    'hispanic': (('Alba', 'Nico', 'Elena', 'Mateo', 'Ines', 'Hugo', 'Lina', 'Leo'), ('Vega', 'Solis', 'Rios', 'Luna', 'Sierra', 'Prado', 'Vidal', 'Cruz')),
    'japanese': (('Aoi', 'Haru', 'Ren', 'Yuki', 'Sora', 'Nao', 'Mio', 'Riku'), ('Mori', 'Aoki', 'Kato', 'Sato', 'Ito', 'Hara', 'Kubo', 'Wada')),
}
ADJECTIVES = ('Quiet', 'Amber', 'Silver', 'Velvet', 'Hidden', 'Copper', 'Solar', 'Indigo', 'Brisk', 'Golden', 'Gentle', 'Lucid')
NOUNS = ('Finch', 'Comet', 'Willow', 'Orchid', 'Harbor', 'Badger', 'Cedar', 'Otter', 'Quartz', 'Meadow', 'Lantern', 'Robin')
PATTERN_ALPHABETS = {'A': string.ascii_uppercase, 'a': string.ascii_lowercase, '9': string.digits, 'X': string.ascii_uppercase + string.digits}


def available(alphabet: str, excluded: str) -> str:
    return ''.join(c for c in dict.fromkeys(alphabet) if c not in excluded)


def character_groups(options: StringsConfig) -> list[str]:
    if options.alphabet == 'hex':
        groups = ['0123456789abcdef']
    elif options.alphabet == 'base64url':
        groups = [string.ascii_letters + string.digits + '-_']
    elif options.alphabet == 'custom':
        groups = [options.custom_chars]
    else:
        groups = []
        if options.lower:
            groups.append(string.ascii_lowercase)
        if options.upper:
            groups.append(string.ascii_uppercase)
        if options.numbers:
            groups.append(string.digits)
        # Usernames and local parts have their own portable alphabet. Symbols
        # are deliberately confined to password / token / identifier modes.
        if options.symbols and options.kind not in {'username', 'localpart', 'email'}:
            groups.append(options.symbols)
    excluded = options.exclude + (AMBIGUOUS if options.exclude_ambiguous else '')
    groups = [available(group, excluded) for group in groups]
    if not groups or any(not group for group in groups):
        raise ValueError('Select a non-empty alphabet; exclusions must leave every selected group available.')
    if options.kind in {'username', 'localpart', 'email'}:
        safe = string.ascii_letters + string.digits + '-_'
        if any(c not in safe for group in groups for c in group):
            raise ValueError('Usernames and email local parts accept only ASCII letters, digits, hyphens and underscores.')
    return groups


def secure_string(length: int, groups: list[str]) -> str:
    """Ensure each chosen group is represented; use OS entropy at every step."""
    alphabet = ''.join(dict.fromkeys(''.join(groups)))
    chars = [secrets.choice(group) for group in groups]
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return ''.join(chars)


def identity_rows(options: IdentityConfig) -> list[dict]:
    given_names, family_names = NAMES[options.region]
    rows = []
    for _ in range(options.count):
        given, family = secrets.choice(given_names), secrets.choice(family_names)
        if options.kind == 'pseudonym':
            given, family = secrets.choice(ADJECTIVES), secrets.choice(NOUNS)
        parts = [given, family]
        if options.convention == 'family_given':
            parts.reverse()
        elif options.convention == 'initial_family':
            parts[0] = given[0] + '.'
        value = options.separator.join(parts)
        value += ''.join(secrets.choice(string.digits) for _ in range(options.suffix_digits))
        rows.append({'value': value, 'kind': options.kind, 'region_inspiration': options.region, 'fictional': True})
    return rows


def string_rows(options: StringsConfig) -> list[dict]:
    groups = character_groups(options)
    rows = []
    for _ in range(options.count):
        value = secure_string(options.length, groups)
        if options.kind == 'email':
            value += '@' + options.domain
        rows.append({'value': value, 'kind': options.kind})
    return rows


def obfuscation_rows(options: ObfuscationConfig) -> list[dict]:
    if options.kind not in {'plate', 'pattern'}:
        return string_rows(StringsConfig(**options.string_options()))
    excluded = options.exclude + (AMBIGUOUS if options.exclude_ambiguous else '')
    alphabets = {key: available(value, excluded) for key, value in PATTERN_ALPHABETS.items()}
    return [{'value': ''.join(secrets.choice(alphabets[c]) if c in alphabets else c for c in options.pattern),
             'kind': options.kind, 'fictional': True} for _ in range(options.count)]


def digits(count):
    return ''.join(secrets.choice(string.digits) for _ in range(count))


def address_rows(options: AddressConfig) -> list[dict]:
    rows = []
    countries = {'US': 'United States', 'GB': 'United Kingdom', 'CA': 'Canada', 'DE': 'Germany', 'AU': 'Australia', 'JP': 'Japan'}
    for _ in range(options.count):
        number = str(secrets.randbelow(999) + 1)
        name = secrets.choice(('Willow', 'Cedar', 'Orchard', 'Meadow', 'Harbor', 'Maple'))
        street, city, region = f'{number} Fictional {name} Lane', 'Example Town', 'Test Region'
        unit = 'Unit ' + str(secrets.randbelow(90) + 1) if options.include_unit else ''
        if options.country == 'US':
            postal = digits(5)
            region = secrets.choice(('CA', 'NY', 'WA', 'OR'))
        elif options.country == 'GB':
            postal = secrets.choice(('AB', 'EX', 'OX')) + digits(1) + ' ' + digits(1) + ''.join(secrets.choice('ABDEFGHJLNPQRSTUWXYZ') for _ in range(2))
            region = 'Example County'
        elif options.country == 'CA':
            letters = 'ABCEGHJKLMNPRSTVXY'
            postal = ''.join(secrets.choice(letters) if i % 2 == 0 else digits(1) for i in range(6))
            postal = postal[:3] + ' ' + postal[3:]
            region = secrets.choice(('ON', 'BC', 'NS'))
        elif options.country == 'DE':
            postal, street, city = digits(5), f'Fiktive Beispielstrasse {number}', 'Beispielstadt'
        elif options.country == 'AU':
            postal, region = digits(4), secrets.choice(('NSW', 'VIC', 'QLD'))
        else:
            postal, street, city, region = digits(3) + '-' + digits(4), f'{number}-1-1 Fictional District', 'Example-shi', 'Example-ken'
        record = {'street': street, 'unit': unit, 'city': city, 'region': region, 'postal_code': postal, 'country': countries[options.country]}
        def joined(keys):
            return ' '.join(record[key] for key in keys if key in options.fields and record[key])
        if options.country == 'JP':
            lines = [joined(['country']), joined(['postal_code']), joined(['region', 'city', 'street']), joined(['unit'])]
        elif options.country == 'DE':
            lines = [joined(['street']), joined(['unit']), joined(['postal_code', 'city']), joined(['region']), joined(['country'])]
        elif options.country in {'US', 'CA', 'AU'}:
            lines = [joined(['street']), joined(['unit']), joined(['city', 'region', 'postal_code']), joined(['country'])]
        else:
            lines = [joined([key]) for key in ('street', 'unit', 'city', 'region', 'postal_code', 'country')]
        row = {'value': ', '.join(line for line in lines if line), 'fictional': True}
        row.update({key: record[key] for key in options.fields})
        rows.append(row)
    return rows


RUNNERS = {'identity': identity_rows, 'strings': string_rows, 'obfuscation': obfuscation_rows, 'addresses': address_rows}


def validate_options(tool: str, options: dict) -> Config:
    if tool not in MODELS:
        raise ValueError('Unknown generator.')
    return MODELS[tool].model_validate(options)


def generate(tool: str, options: dict) -> dict:
    config = validate_options(tool, options)
    rows = RUNNERS[tool](config)
    secret = tool in {'strings', 'obfuscation'} and config.kind in {'password', 'token'}
    fictional = tool in {'identity', 'addresses'} or (tool == 'obfuscation' and config.kind in {'plate', 'pattern'})
    return {'tool': tool, 'schema_version': 1, 'count': len(rows), 'columns': list(rows[0]), 'rows': rows,
            'secret': secret, 'fictional': fictional, 'persisted': False,
            'notice': NOTICE if fictional else 'Generated locally with cryptographically secure randomness.',
            'expires_in_seconds': 60}


def catalog() -> dict:
    return {'schema_version': 1, 'max_count': 1000, 'exports': ['json', 'csv', 'txt'],
            'tools': {tool: {**TOOL_INFO[tool], 'defaults': model().model_dump(), 'schema': model.model_json_schema()}
                      for tool, model in MODELS.items()}}


def csv_cell(value) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)
    # Spreadsheet programs may ignore leading whitespace before a formula.
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


def export_result(result: dict, format: str) -> str:
    """Pure in-memory serializer; no files, paths, store, queue or logging."""
    rows, columns = result['rows'], result['columns']
    if not 1 <= len(rows) <= 1000 or len(columns) > 16:
        raise ValueError('Export exceeds generator limits.')
    if format == 'json':
        return json.dumps(result, ensure_ascii=False, indent=2)
    if format == 'txt':
        return (result.get('notice', '') + '\n' if result.get('fictional') else '') + '\n'.join(str(row['value']) for row in rows) + '\n'
    if format != 'csv':
        raise ValueError('Choose JSON, CSV or TXT export.')
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows([[csv_cell(row.get(column, '')) for column in columns] for row in rows])
    return buffer.getvalue()
