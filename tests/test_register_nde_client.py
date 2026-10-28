import pytest
import requests

from oai_to_nde.register_nde_client import (
    NdeDatasetRegisterClient,
    NdeDomainNotAllowedError,
    NdeRegisterError,
    NdeUnresolvableUrlError,
    NdeValidationResult,
)

SHACL = "http://www.w3.org/ns/shacl#"


def shacl_result(severity, path, *messages):
    return {
        "@id": "_:b",
        SHACL + "resultSeverity": [{"@id": SHACL + severity}],
        SHACL + "resultPath": [{"@id": path}],
        SHACL + "resultMessage": [{"@value": text, "@language": lang} for lang, text in messages],
    }


def test_violations_lists_only_violations_with_english_messages():
    report = [
        {"@id": "_:report", "@type": [SHACL + "ValidationReport"]},
        shacl_result("Warning", "https://schema.org/name", ("en", "A warning")),
        shacl_result("Violation", "https://schema.org/publisher", ("nl", "Voeg een uitgever toe"), ("en", "Add a publisher")),
        shacl_result("Violation", "https://schema.org/publisher", ("nl", "Voeg een uitgever toe"), ("en", "Add a publisher")),
        shacl_result("Violation", "http://example.org/ns#license", ("nl", "Alleen Nederlands")),
    ]
    assert NdeValidationResult(False, report).violations == ["publisher: Add a publisher", "license: Alleen Nederlands"]


def test_violations_is_empty_for_a_non_jsonld_report():
    assert NdeValidationResult(False, "_:b1 a sh:ValidationReport.").violations == []
    assert NdeValidationResult(True, None).violations == []


class FakeResponse:
    def __init__(self, status_code, text="", content_type="application/ld+json", json_body=None):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}
        self._json = json_body

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json


@pytest.fixture
def client(monkeypatch):
    client = NdeDatasetRegisterClient(base_url="http://register/api/")
    client.sent = []
    client.reply = FakeResponse(200)

    def fake_request(method, url, **kwargs):
        client.sent.append((method, url, kwargs))
        if isinstance(client.reply, Exception):
            raise client.reply
        return client.reply

    monkeypatch.setattr(client._http, "request", fake_request)
    return client


def test_register_accepted(client):
    client.reply = FakeResponse(202, json_body=[])
    result = client.register("https://x/d")
    assert result.valid
    method, url, kwargs = client.sent[0]
    assert (method, url, kwargs["json"]) == ("POST", "http://register/api/datasets", {"@id": "https://x/d"})


def test_register_invalid_returns_report_instead_of_raising(client):
    client.reply = FakeResponse(400, json_body=[{"report": 1}])
    result = client.register("https://x/d")
    assert not result.valid
    assert result.report == [{"report": 1}]


def test_validate_url_uses_put(client):
    client.reply = FakeResponse(200, json_body=[])
    assert client.validate_url("https://x/d").valid
    assert client.sent[0][0:2] == ("PUT", "http://register/api/datasets/validate")


def test_validate_body_picks_content_type(client):
    client.reply = FakeResponse(200, json_body=[])
    client.validate_body("@prefix a: <b> .")
    client.validate_body({"@id": "x"})
    assert client.sent[0][2]["headers"]["Content-Type"] == "text/turtle"
    assert client.sent[1][2]["headers"]["Content-Type"] == "application/ld+json"


def test_status_codes_map_to_exceptions(client):
    client.reply = FakeResponse(403, "not allowed")
    with pytest.raises(NdeDomainNotAllowedError):
        client.register("https://x/d")
    client.reply = FakeResponse(406, "no datasets")
    with pytest.raises(NdeUnresolvableUrlError):
        client.register("https://x/d")
    client.reply = FakeResponse(504, "Gateway Timeout")
    with pytest.raises(NdeRegisterError) as excinfo:
        client.register("https://x/d")
    assert excinfo.value.status_code == 504


def test_connection_errors_are_wrapped(client):
    client.reply = requests.ConnectionError("Remote end closed connection")
    with pytest.raises(NdeRegisterError) as excinfo:
        client.validate_url("https://x/d")
    assert excinfo.value.status_code == 0


def test_is_registered_treats_404_as_false(client):
    client.reply = FakeResponse(404)
    assert client.is_registered("https://x/d") is False
    client.reply = FakeResponse(200)
    assert client.is_registered("https://x/d") is True
