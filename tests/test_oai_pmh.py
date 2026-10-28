import io
import sys

import pytest
import requests
import urllib3

from oai_to_nde import oai_pmh
from oai_to_nde.oai_pmh import OAIClient, OAIError

OAI = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">{}</OAI-PMH>'


def header(identifier, deleted=False):
    status = ' status="deleted"' if deleted else ""
    return f"<header{status}><identifier>{identifier}</identifier></header>"


def page(*headers, token=None):
    token_xml = f"<resumptionToken>{token}</resumptionToken>" if token else ""
    return OAI.format(f"<ListIdentifiers>{''.join(headers)}{token_xml}</ListIdentifiers>")


class FakeResponse:
    def __init__(self, xml):
        self.raw = io.BytesIO(xml.encode())

    def raise_for_status(self):
        pass

    def close(self):
        pass


@pytest.fixture
def serve(monkeypatch):
    """Serve the given XML documents in order and record the request params."""

    def install(*documents):
        remaining = list(documents)
        requests_made = []

        def fake_get(url, params=None, **kwargs):
            requests_made.append(params)
            return FakeResponse(remaining.pop(0))

        monkeypatch.setattr(oai_pmh.requests, "get", fake_get)
        return requests_made

    return install


def test_follows_resumption_tokens(serve):
    requests_made = serve(page(header("a"), header("b"), token="T1"), page(header("c")))
    assert list(OAIClient("http://x/oai").iter_identifiers()) == ["a", "b", "c"]
    assert requests_made[1] == {"verb": "ListIdentifiers", "resumptionToken": "T1"}


def test_skips_deleted_records(serve):
    serve(page(header("a"), header("gone", deleted=True)))
    assert list(OAIClient("http://x/oai").iter_identifiers()) == ["a"]


def test_passes_set_and_from_date(serve):
    requests_made = serve(page(header("a")))
    list(OAIClient("http://x/oai").iter_identifiers(oai_set="s", from_date="2026-10-01"))
    assert requests_made[0]["set"] == "s"
    assert requests_made[0]["from"] == "2026-10-01"


def test_no_records_match_is_an_empty_result(serve):
    serve(OAI.format('<error code="noRecordsMatch"/>'))
    assert list(OAIClient("http://x/oai").iter_identifiers()) == []


def test_other_oai_errors_are_raised(serve):
    serve(OAI.format('<error code="badArgument">The set does not exist</error>'))
    with pytest.raises(OAIError) as excinfo:
        list(OAIClient("http://x/oai").iter_identifiers(oai_set="nope"))
    assert excinfo.value.code == "badArgument"
    assert "The set does not exist" in str(excinfo.value)


def test_truncated_xml_is_an_oai_error(serve):
    serve(page(header("a"))[:-30])
    with pytest.raises(OAIError) as excinfo:
        list(OAIClient("http://x/oai").iter_identifiers())
    assert excinfo.value.code == "invalidXml"


def test_dropped_connection_is_a_requests_error(monkeypatch):
    class DroppingRaw:
        def __init__(self):
            self.chunks = [page(header("a")).encode()[:60]]

        def read(self, size=-1):
            if self.chunks:
                return self.chunks.pop()
            raise urllib3.exceptions.ProtocolError("Connection broken")

    response = FakeResponse("")
    response.raw = DroppingRaw()
    monkeypatch.setattr(oai_pmh.requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(requests.RequestException):
        list(OAIClient("http://x/oai").iter_identifiers())


def test_blank_resumption_token_ends_the_harvest(serve):
    requests_made = serve(OAI.format("<ListIdentifiers>" + header("a") + "<resumptionToken>\n </resumptionToken></ListIdentifiers>"))
    assert list(OAIClient("http://x/oai").iter_identifiers()) == ["a"]
    assert len(requests_made) == 1


def test_retrieve_identifiers_writes_one_per_line(serve, tmp_path):
    serve(page(header("a"), header("b")))
    output = tmp_path / "ids.txt"
    oai_pmh.retrieve_identifiers("http://x/oai", output)
    assert output.read_text() == "a\nb\n"


def test_oai_get_ids_command_passes_its_options_on(monkeypatch):
    seen = {}
    monkeypatch.setattr(oai_pmh, "retrieve_identifiers", lambda url, output, **kwargs: seen.update(url=url, output=output, **kwargs))
    monkeypatch.setattr(
        sys, "argv", ["oai-get-ids", "http://x/oai", "--set", "s", "--from-date", "2026-10-01", "--output-file", "ids.txt"]
    )
    oai_pmh.main()
    assert seen == {
        "url": "http://x/oai",
        "output": "ids.txt",
        "oai_set": "s",
        "metadata_prefix": "oai_dc",
        "from_date": "2026-10-01",
    }
