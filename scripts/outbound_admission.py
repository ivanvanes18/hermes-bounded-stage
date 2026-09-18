"""One fail-closed boundary for context and semantic evidence.

The existing Hermes canonical redactor is mandatory. The trusted parent must
supply its existing data-policy classifier and exact content grants out of band;
no classifier, fixture bypass, public-data inference or automatic grant is
provided here. Grants are authority records, not an OS isolation mechanism.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass
import time
from schema_validation import ContractError,canonical,digest,loads
from typed_jev import _request,valid_grant

PURPOSES = frozenset({'context_reranking', 'semantic_cascade', 'executor_routing', 'triage', 'stage_transition'})
CLASSIFICATIONS = frozenset({'synthetic', 'public-redacted'})


class AdmissionError(ContractError):
    """Messages are fixed codes; never include evidence or callback exceptions."""


def canonical_redact(text):
    try:
        from agent.redact import redact_sensitive_text
        result = redact_sensitive_text(text, force=True, redact_url_credentials=True)
        if not isinstance(result, str):
            raise TypeError
        return result
    except Exception:
        raise AdmissionError('canonical_redactor_unavailable_or_failed') from None


def _redact(value):
    if isinstance(value, str):
        result = canonical_redact(value)
        if not isinstance(result, str):
            raise AdmissionError('canonical_redactor_invalid')
        return result
    if type(value) is list:
        return [_redact(item) for item in value]
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or _redact(key) != key:
                raise AdmissionError('outbound_identifier_redaction')
            result[key] = _redact(item)
        return result
    if value is None or type(value) in (bool, int, float):
        return value
    raise AdmissionError('outbound_value_type')


@dataclass(frozen=True)
class PreparedRequest:
    body: bytes
    purpose: str
    classification: str

    def metadata(self):
        return {'payload_sha256':digest(loads(self.body)), 'purpose':self.purpose,
                'classification':self.classification}


class OutboundAdmission:
    def __init__(self, *, classifier=None, grants=()):
        self.classifier = classifier
        # No automatic grants, no access to provider credentials. Invalid grants
        # are preserved as invalid so admission, not construction, denies them.
        self.grants = copy.deepcopy(tuple(grants))

    def _grant(self, prepared):
        payload = loads(prepared.body)
        for grant in self.grants:
            if (valid_grant(grant, payload, prepared.purpose) and
                    grant['classification'] == prepared.classification):
                return grant
        raise AdmissionError('outbound_grant_missing_expired_or_mismatched')

    def prepare(self, requests, *, purpose, classification, documents=(), source_guard=None):
        """Classify full local documents; redact exact wire payloads; preflight ALL grants."""
        if purpose not in PURPOSES or classification not in CLASSIFICATIONS:
            raise AdmissionError('outbound_classification_or_purpose_denied')
        if not callable(self.classifier):
            raise AdmissionError('outbound_classifier_unavailable')
        if source_guard is not None:
            source_guard()
        try:
            # Freeze inputs before invoking trusted callbacks. Classification is
            # based on raw full documents as well as objective/evidence/questions.
            raw = loads(canonical({'documents':list(documents),
                                   'requests':[_request(s, q) for s, q in requests]}))
            actual = self.classifier(copy.deepcopy(raw))
            if not isinstance(actual, str) or actual not in CLASSIFICATIONS or actual != classification:
                raise AdmissionError('outbound_classification_denied')
            prepared = []
            for payload in raw['requests']:
                redacted = _redact(payload)
                checked = _request(redacted['state'], redacted['questions'])
                if redacted != checked:
                    raise AdmissionError('outbound_redacted_payload_invalid')
                item = PreparedRequest(canonical(checked), purpose, classification)
                self._grant(item)
                prepared.append(item)
            if not prepared:
                raise AdmissionError('outbound_empty_batch')
        except AdmissionError:
            raise
        except Exception:
            raise AdmissionError('outbound_policy_or_redactor_failed') from None
        # A classifier/redactor must not make the earlier source read stale.
        if source_guard is not None:
            source_guard()
        return tuple(prepared)

    def evaluate(self, prepared, provider, *, purpose, classification, source_guard=None, on_send=None):
        if (type(prepared) is not PreparedRequest or prepared.purpose != purpose or
                prepared.classification != classification or purpose not in PURPOSES or
                classification not in CLASSIFICATIONS):
            raise AdmissionError('outbound_prepared_binding')
        if source_guard is not None:
            source_guard()
        self._grant(prepared)  # expiry/hash/purpose/class checked at the send boundary
        payload = loads(prepared.body)  # fresh copy: no mutable raw-payload alias
        if on_send is not None:
            on_send()
        return provider.evaluate(payload['state'], payload['questions'])


def require_admission(admission):
    if type(admission) is not OutboundAdmission:
        raise AdmissionError('outbound_admission_missing')
    return admission
