"""Small offline, coherent formatting fixtures, never deliverability/identity verification.

Cities/regions/postal codes are sample geographic groups, streets are explicitly
fictional. No lookup or external address data is used. Regeneration never mixes
locked geographic fields with an incompatible group.
"""
from __future__ import annotations

import secrets

COUNTRIES={'US':'United States','GB':'United Kingdom','CA':'Canada','AU':'Australia','DE':'Germany','JP':'Japan'}
# city, region, postal code, county. A small reviewed sample, not a complete postal database.
LOCATIONS={
 'US':[('Pasadena','CA','91101','Los Angeles'),('Pasadena','CA','91103','Los Angeles'),('Los Angeles','CA','90012','Los Angeles'),
       ('Seattle','WA','98101','King'),('Seattle','WA','98104','King'),('Portland','OR','97201','Multnomah'),('Albany','NY','12207','Albany')],
 'GB':[('London','England','SW1A 1AA','Greater London'),('London','England','SW1A 2AA','Greater London'),
       ('Oxford','England','OX1 1AA','Oxfordshire'),('Edinburgh','Scotland','EH1 1YZ','City of Edinburgh')],
 'CA':[('Toronto','ON','M5V 2T6','Toronto'),('Toronto','ON','M5H 2N2','Toronto'),('Ottawa','ON','K1A 0B1','Ottawa'),
       ('Vancouver','BC','V6B 1A1','Metro Vancouver'),('Halifax','NS','B3J 1S9','Halifax')],
 'AU':[('Sydney','NSW','2000',''),('Parramatta','NSW','2150',''),('Melbourne','VIC','3000',''),('Brisbane','QLD','4000','')],
 'DE':[('Berlin','Berlin','10115',''),('Berlin','Berlin','10117',''),('Hamburg','Hamburg','20095',''),('Munich','Bavaria','80331','')],
 'JP':[('Chiyoda','Tokyo','100-0001',''),('Chiyoda','Tokyo','100-0005',''),('Shinjuku','Tokyo','160-0022',''),('Kita','Osaka','530-0001','')],
}
GEOGRAPHIC={'country','city','region','postal_code','county'}
FIELDS=('street','unit','city','region','postal_code','country','county','phone')


def formatted(record,code,fields):
    def join(keys,separator=' '): return separator.join(record.get(k,'') for k in keys if k in fields and record.get(k))
    if code=='JP': groups=[['country'],['postal_code'],['region','city','street'],['unit']]
    elif code=='DE': groups=[['street'],['unit'],['postal_code','city'],['region'],['country']]
    elif code in {'US','CA','AU'}: groups=[['street'],['unit'],['city','region','postal_code'],['country']]
    else: groups=[['street'],['unit'],['city'],['region'],['postal_code'],['country']]
    return ', '.join(value for keys in groups if (value:=join(keys)))


def address(options,current=None,locked=(),target=None):
    current=current or {};locked=set(locked)
    if target in locked: raise ValueError('Unlock this field before regenerating it.')
    code=options['country']
    if (target=='country' or current.get('country',COUNTRIES[code])!=COUNTRIES[code]) and locked&{'street','phone','unit'}:
        raise ValueError('Unlock street, unit and phone fields before changing country so their formats can stay consistent.')
    if 'country' in locked and current.get('country')!=COUNTRIES[code]:
        raise ValueError('The country field is locked. Unlock it before changing country format.')
    countries=[code]
    if target=='country': countries=[c for c in COUNTRIES if COUNTRIES[c]!=current.get('country')]
    candidates=[]
    for candidate in countries:
        for city,region,postal,county in LOCATIONS[candidate]:
            geo={'city':city,'region':region,'postal_code':postal,'county':county,'country':COUNTRIES[candidate]}
            if any(geo[field]!=current.get(field,'') for field in locked&GEOGRAPHIC): continue
            if target in GEOGRAPHIC and geo[target]==current.get(target): continue
            candidates.append((candidate,geo))
    if not candidates:
        raise ValueError('No compatible alternative in this country sample. Unlock a related geographic field or keep the current value.')
    if target and target not in GEOGRAPHIC:
        candidates=[c for c in candidates if all(current.get(k,'')==v for k,v in c[1].items() if k in current)]
        if not candidates: raise ValueError('Current geographic fields do not match the selected country sample. Regenerate All or unlock the location fields.')
    code,geo=secrets.choice(candidates)
    # Never generate routable personal phone numbers. These reserved examples are
    # explicitly labelled test values; unavailable country reservations stay blank.
    phone={
        'US':lambda:'+1 202 555 01'+f'{secrets.randbelow(100):02d}',
        'CA':lambda:'+1 416 555 01'+f'{secrets.randbelow(100):02d}',
        'GB':lambda:'+44 1632 960'+f'{secrets.randbelow(1000):03d}',
        'AU':lambda:'+61 2 5550 '+f'{secrets.randbelow(1000):04d}',
    }
    number=secrets.randbelow(999)+1
    street=f'{number} Fictional {secrets.choice(("Willow","Cedar","Meadow","Harbor"))} Lane'
    if code=='DE': street=f'Fiktive Beispielstrasse {number}'
    elif code=='JP': street=f'{number}-1-1 Fictional District'
    record={**geo,'street':street,'unit':('Unit '+str(secrets.randbelow(99)+1)) if options.get('include_unit') else '',
            'phone':phone[code]() if options.get('include_phone') and code in phone else ''}
    if target in {'unit','phone'} and not record[target]:
        raise ValueError('Enable this optional field first; test phone numbers are available for US, UK, Canada and Australia only.')
    if target and target not in GEOGRAPHIC and record[target]==current.get(target):
        if target=='unit': record[target]='Unit '+str((int(record[target].split()[-1])%99)+1)
        elif target=='phone': record[target]=record[target][:-1]+str((int(record[target][-1])+1)%10)
        else: record[target]=record[target].replace(str(number),str(number%999+1),1)
    if target and target not in GEOGRAPHIC:
        record={**current,target:record[target]}
    elif target in GEOGRAPHIC:
        record={**current,**geo}
        if target=='country': record.update(street=street,unit=record.get('unit',''),phone=phone[code]() if options.get('include_phone') and code in phone else '')
    for field in locked: record[field]=current.get(field,'')
    fields=list(options['fields'])
    if options.get('include_county') and 'county' not in fields: fields.append('county')
    if options.get('include_phone') and 'phone' not in fields: fields.append('phone')
    return {'value':formatted(record,code,fields),'fictional':True,**{k:record.get(k,'') for k in fields}}
