"""Closed JSON contracts, using a documented strict subset of JSON Schema 2020-12.

This is not a general-purpose JSON Schema implementation. Unsupported keywords,
remote references, duplicate JSON keys and non-finite numbers are rejected.
All bundled schemas stay within this subset; no dependency is installed.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
from typing import Any

MAX_JSON_BYTES = 1_048_576
KEYWORDS = frozenset({'$schema','$id','$defs','$ref','$comment','title','description',
    'type','properties','required','additionalProperties','items','uniqueItems',
    'minItems','maxItems','minLength','maxLength','minProperties','maxProperties',
    'pattern','enum','const','minimum','maximum','anyOf','oneOf','allOf'})

class ContractError(ValueError):
    """Public reason code only; never include the rejected payload."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ContractError('duplicate_json_key')
        result[key] = value
    return result


def loads(raw: str | bytes) -> Any:
    if not isinstance(raw,(str,bytes)) or len(raw) > MAX_JSON_BYTES:
        raise ContractError('invalid_json_size')
    try:
        return json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite_json')))
    except (ValueError,UnicodeError,RecursionError) as e:
        if isinstance(e,ContractError): raise
        raise ContractError('invalid_json') from None


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value,sort_keys=True,ensure_ascii=False,
                          separators=(',',':'),allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise ContractError('non_json_value') from None


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _schema_check(node,root,depth=0):
    if depth>40 or not isinstance(node,dict) or set(node)-KEYWORDS:
        raise ContractError('unsupported_schema')
    if '$ref' in node:
        ref=node['$ref']
        if not isinstance(ref,str) or not re.fullmatch(r'#/\$defs/[a-zA-Z0-9_-]+',ref):
            raise ContractError('unsupported_schema_reference')
        if ref.split('/')[-1] not in root.get('$defs',{}):
            raise ContractError('missing_schema_reference')
    for key in ('$defs','properties'):
        for child in node.get(key,{}).values(): _schema_check(child,root,depth+1)
    for key in ('items','additionalProperties'):
        if isinstance(node.get(key),dict): _schema_check(node[key],root,depth+1)
    for key in ('anyOf','oneOf','allOf'):
        for child in node.get(key,[]): _schema_check(child,root,depth+1)


def _matches_type(value,kind):
    return {'object':type(value) is dict,'array':type(value) is list,
            'string':type(value) is str,'integer':type(value) is int,
            'number':type(value) in (int,float) and math.isfinite(value),
            'boolean':type(value) is bool,'null':value is None}.get(kind,False)


def _check(value,node,root,depth=0):
    if depth>40: raise ContractError('contract_depth')
    if '$ref' in node: _check(value,root['$defs'][node['$ref'].split('/')[-1]],root,depth+1)
    if 'type' in node:
        kinds=node['type'] if isinstance(node['type'],list) else [node['type']]
        if not any(_matches_type(value,k) for k in kinds): raise ContractError('contract_type')
    if 'enum' in node and canonical(value) not in [canonical(x) for x in node['enum']]:
        raise ContractError('contract_enum')
    if 'const' in node and canonical(value)!=canonical(node['const']): raise ContractError('contract_const')
    for combination in ('anyOf','oneOf','allOf'):
        if combination in node:
            successes=0
            for child in node[combination]:
                try: _check(value,child,root,depth+1); successes+=1
                except ContractError: pass
            if (combination=='anyOf' and successes==0 or
                combination=='oneOf' and successes!=1 or
                combination=='allOf' and successes!=len(node[combination])):
                raise ContractError('contract_combination')
    if type(value) is dict:
        if any(type(k) is not str for k in value): raise ContractError('contract_key')
        if not set(node.get('required',[]))<=set(value): raise ContractError('contract_required')
        if not node.get('minProperties',0)<=len(value)<=node.get('maxProperties',10000):
            raise ContractError('contract_properties_count')
        props=node.get('properties',{})
        for key,item in value.items():
            if key in props: _check(item,props[key],root,depth+1)
            elif node.get('additionalProperties',True) is False: raise ContractError('contract_unknown_field')
            elif isinstance(node.get('additionalProperties'),dict): _check(item,node['additionalProperties'],root,depth+1)
    if type(value) is list:
        if not node.get('minItems',0)<=len(value)<=node.get('maxItems',10000): raise ContractError('contract_items_count')
        if node.get('uniqueItems') and len({canonical(x) for x in value})!=len(value): raise ContractError('contract_duplicate_item')
        for item in value:
            if 'items' in node: _check(item,node['items'],root,depth+1)
    if type(value) is str:
        if '\x00' in value or not node.get('minLength',0)<=len(value)<=node.get('maxLength',MAX_JSON_BYTES):
            raise ContractError('contract_string')
        if 'pattern' in node and re.search(node['pattern'],value) is None: raise ContractError('contract_pattern')
    if type(value) in (int,float):
        if not math.isfinite(value) or not node.get('minimum',-math.inf)<=value<=node.get('maximum',math.inf):
            raise ContractError('contract_number')


def validate(value: Any,schema: dict) -> None:
    if len(canonical(value))>MAX_JSON_BYTES: raise ContractError('contract_size')
    try:
        _schema_check(schema,schema)
        _check(value,schema,schema)
    except (TypeError,KeyError,RecursionError,re.error):
        raise ContractError('invalid_schema') from None
