"""Pinned, bounded stdlib TypeSafe transport; no network without a content grant.

The parent supplies grants out of band. A grant is an authorization record, NOT
an unforgeable credential. Workers must not be able to construct/modify parent
arguments or this code. Unknown provider fields fail closed. No retries,
redirects, proxy inheritance, body logging or implicit provider substitution.
"""
from __future__ import annotations
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
try:
    from .schema_validation import ContractError,canonical,digest,loads
except ImportError:
    from schema_validation import ContractError,canonical,digest,loads
MODEL='jev-1.13.0'
ENDPOINT='https://api.typesafe.ai/v1/systemone'
MAX_BODY=128*1024
PURPOSES={'skill_routing','executor_routing','context_reranking','semantic_cascade','triage','stage_transition'}
class JevError(ValueError): pass

def probability(v):return type(v) in (int,float) and math.isfinite(v) and 0<=v<=1

def _request(state,questions):
    if type(state) is not dict or type(questions) is not dict or not 1<=len(questions)<=64:
        raise JevError('request_shape')
    for k,q in questions.items():
        if not isinstance(k,str) or not 1<=len(k)<=128 or not isinstance(q,dict) or set(q)!={'type','instructions','criteria'}:
            raise JevError('question_shape')
        if q['type'] not in ('choice','noul') or not isinstance(q['instructions'],str) or not 1<=len(q['instructions'])<=4000:
            raise JevError('question_shape')
        if not isinstance(q['criteria'],dict) or not 1<=len(q['criteria'])<=64: raise JevError('criteria_shape')
        if q['type']=='noul' and set(q['criteria'])!={'true','false'}:raise JevError('noul_criteria')
        if any(not isinstance(x,str) or not 1<=len(x)<=128 or (v is not None and (not isinstance(v,str) or len(v)>4000)) for x,v in q['criteria'].items()):
            raise JevError('criteria_shape')
    payload={'model':MODEL,'state':state,'questions':questions}
    if len(canonical(payload))>MAX_BODY:raise JevError('payload_too_large')
    return payload

def validate_reply(reply,questions):
    if not isinstance(reply,dict) or set(reply)!={'model','answers','usage'} or reply['model']!=MODEL:
        raise JevError('model_or_envelope')
    answers=reply['answers'];usage=reply['usage']
    if not isinstance(answers,dict) or set(answers)!=set(questions):raise JevError('answer_coverage')
    if not isinstance(usage,dict) or set(usage)!={'input_tokens','output_tokens'} or any(type(v) is not int or not 0<=v<=1_000_000_000 for v in usage.values()):
        raise JevError('usage_shape')
    for name,q in questions.items():
        a=answers[name]
        if not isinstance(a,dict) or a.get('type')!=q['type']:raise JevError('answer_type')
        if q['type']=='noul':
            if set(a)!={'type','noul'} or not probability(a['noul']):raise JevError('noul_shape')
        elif q['type']=='choice':
            if set(a)!={'type','choice','probabilities','confidence'}:raise JevError('choice_shape')
            p=a['probabilities']
            if not isinstance(p,dict) or set(p)!=set(q['criteria']) or a['choice'] not in p:
                raise JevError('option_coverage')
            if not all(probability(x) for x in p.values()) or not probability(a['confidence']):raise JevError('invalid_probability')
            # Preserve full raw probabilities. Transport rounding does not waive
            # coverage, range, argmax, confidence or controller margin gates.
            if abs(sum(p.values())-1)>.020000001 or p[a['choice']]<max(p.values())-1e-12:
                raise JevError('invalid_distribution')
        else:raise JevError('answer_type')
    return answers

def valid_grant(grant,payload,purpose,now=None):
    try:
        now=time.time() if now is None else now
        return (type(grant) is dict and set(grant)=={'schema_version','approved_by','purpose','classification','payload_sha256','expires_at'}
            and type(grant['schema_version']) is int and grant['schema_version']==1
            and grant['approved_by']=='parent' and purpose in PURPOSES and grant['purpose']==purpose
            and grant['classification'] in ('synthetic','public-redacted')
            and type(grant['expires_at']) in (int,float) and math.isfinite(grant['expires_at'])
            and now<grant['expires_at']<=now+3600 and grant['payload_sha256']==digest(payload))
    except (KeyError,TypeError,ContractError):return False

class TypedJev:
    def __init__(self,*,purpose,grants=(),credential=None,timeout_seconds=3):
        if purpose not in PURPOSES:raise JevError('purpose_not_allowed')
        if type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or not .1<=timeout_seconds<=15:
            raise JevError('invalid_timeout')
        self.purpose=purpose;self.grants=tuple(grants);self.credential=credential;self.timeout_seconds=timeout_seconds
    def evaluate(self,state,questions):
        payload=_request(state,questions)
        if not any(valid_grant(g,payload,self.purpose) for g in self.grants):raise JevError('outbound_not_authorized')
        key=self.credential if self.credential is not None else os.environ.get('TYPESAFE_API_KEY','')
        if not isinstance(key,str) or not 16<=len(key)<=512 or not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
            raise JevError('credential_unavailable')
        env={'TYPESAFE_API_KEY':key,'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
        try:
            p=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--http'],input=canonical(payload),
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=self.timeout_seconds,check=False)
            if p.returncode or len(p.stdout)>MAX_BODY:raise JevError('provider_unavailable')
            reply=loads(p.stdout);validate_reply(reply,questions);return reply
        except (OSError,subprocess.TimeoutExpired,ContractError):raise JevError('provider_unavailable') from None

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise JevError('redirect_refused')

def _http_main():
    try:
        raw=sys.stdin.buffer.read(MAX_BODY+1)
        if len(raw)>MAX_BODY:return 2
        p=loads(raw)
        if set(p)!={'model','state','questions'} or p['model']!=MODEL:return 2
        _request(p['state'],p['questions'])
        key=os.environ.get('TYPESAFE_API_KEY','')
        if not key:return 2
        req=urllib.request.Request(ENDPOINT,data=raw,method='POST',headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
        with opener.open(req,timeout=10) as response:
            if response.status!=200:return 2
            answer=response.read(MAX_BODY+1)
        if len(answer)>MAX_BODY:return 2
        validate_reply(loads(answer),p['questions']);sys.stdout.buffer.write(answer);return 0
    except Exception:return 2
if __name__=='__main__':raise SystemExit(_http_main() if sys.argv[1:]==['--http'] else 2)
