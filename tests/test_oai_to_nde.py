import csv
import itertools
import sys

import pytest

from oai_to_nde import oai_to_nde as pipeline
from oai_to_nde.register_nde_client import (
    NdeDomainNotAllowedError,
    NdeRegisterError,
    NdeUnresolvableUrlError,
    NdeValidationResult,
)


class FakeClient:
    """Stands in for NdeDatasetRegisterClient; `behaviour` maps a URL to a list of outcomes, one per call."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def _respond(self, url):
        self.calls.append(url)
        outcomes = self.behaviour.get(url, [NdeValidationResult(True, None)])
        outcome = outcomes[min(len(self.calls_for(url)) - 1, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def calls_for(self, url):
        return [c for c in self.calls if c == url]

    register = _respond
    validate_url = _respond


@pytest.fixture(autouse=True)
def unique_timestamps(monkeypatch):
    """Runs within one second would otherwise share file names."""
    counter = itertools.count(1)
    monkeypatch.setattr(pipeline, "_timestamp", lambda: f"t{next(counter)}")


def write_urls(tmp_path, *urls):
    path = tmp_path / "urls.txt"
    path.write_text("".join(f"{u}\n" for u in urls))
    return path


def test_file_names_include_host_set_and_from_date():
    oai = "https://example.org/oai"
    assert pipeline.data_path(oai, "ids.txt").as_posix() == "data/example.org_ids.txt"
    assert pipeline.data_path(oai, "urls.txt", "s", "d").as_posix() == "d/example.org_s_urls.txt"
    assert pipeline.data_path(oai, "urls.txt", None, "d", "2026-10-01").as_posix() == "d/example.org_from-2026-10-01_urls.txt"
    assert pipeline.data_path(oai, "retry_t1.txt", "s", "d", "2026-10-01T12:00:00Z").name == "example.org_s_from-2026-10-01T12_00_00Z_retry_t1.txt"


def test_register_urls_sorts_outcomes(tmp_path):
    client = FakeClient({"bad": [NdeValidationResult(False, "report")], "gone": [NdeUnresolvableUrlError(404, "x")]})
    result = pipeline.register_urls(write_urls(tmp_path, "ok", "bad", "gone"), client)
    assert result["accepted"] == ["ok"]
    assert result["invalid"] == ["bad"]
    assert [url for url, _ in result["failed"]] == ["gone"]


def test_validate_only_calls_validate_not_register(tmp_path):
    client = FakeClient()
    client.register = lambda url: pytest.fail("register must not be called")
    result = pipeline.register_urls(write_urls(tmp_path, "ok"), client, validate_only=True)
    assert result["accepted"] == ["ok"]


def test_transient_failures_are_retried(tmp_path):
    client = FakeClient({"flaky": [NdeRegisterError(0, "dropped"), NdeRegisterError(504, "timeout"), NdeValidationResult(True, None)]})
    result = pipeline.register_urls(write_urls(tmp_path, "flaky"), client, retries=2, retry_delay=0)
    assert result["accepted"] == ["flaky"]
    assert len(client.calls_for("flaky")) == 3


def test_failure_remains_after_retries_are_used_up(tmp_path):
    client = FakeClient({"down": [NdeRegisterError(504, "timeout")]})
    result = pipeline.register_urls(write_urls(tmp_path, "down"), client, retries=2, retry_delay=0)
    assert [url for url, _ in result["failed"]] == ["down"]
    assert len(client.calls_for("down")) == 3


def test_permanent_failures_are_not_retried(tmp_path):
    client = FakeClient({"gone": [NdeUnresolvableUrlError(404, "x")]})
    result = pipeline.register_urls(write_urls(tmp_path, "gone"), client, retries=3, retry_delay=0)
    assert [url for url, _ in result["failed"]] == ["gone"]
    assert len(client.calls_for("gone")) == 1


def test_no_retries_by_default(tmp_path):
    client = FakeClient({"down": [NdeRegisterError(504, "timeout")]})
    pipeline.register_urls(write_urls(tmp_path, "down"), client)
    assert len(client.calls_for("down")) == 1


def test_disallowed_domain_stops_the_run(tmp_path):
    client = FakeClient({"a": [NdeDomainNotAllowedError(403, "no")]})
    with pytest.raises(NdeDomainNotAllowedError):
        pipeline.register_urls(write_urls(tmp_path, "a", "b"), client)
    assert client.calls == ["a"]


def run_main(monkeypatch, *args, client=None):
    monkeypatch.setattr(pipeline, "NdeDatasetRegisterClient", lambda: client or FakeClient())
    monkeypatch.setattr(sys, "argv", ["oai-to-nde", "https://example.org/oai", *args])
    pipeline.main()


def test_main_saves_retryable_urls_in_a_timestamped_file(tmp_path, monkeypatch, capsys):
    (tmp_path / "example.org_urls.txt").write_text("good\nbad\n")
    failing = FakeClient({"bad": [NdeRegisterError(504, "timeout")]})
    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path), client=failing)
    assert exit_info.value.code == 1
    (retry_file,) = tmp_path.glob("example.org_retry_*.txt")
    assert retry_file.read_text() == "bad\n"
    assert "Accepted: 1, invalid: 0, failed: 1" in capsys.readouterr().out

    # Retrying from that file leaves the earlier files alone and writes no new retry file when all succeed.
    run_main(monkeypatch, "--urls-file", str(retry_file), "--data-dir", str(tmp_path))
    assert list(tmp_path.glob("example.org_retry_*.txt")) == [retry_file]
    assert len(list(tmp_path.glob("example.org_results_*.csv"))) == 2


def test_main_does_not_offer_permanent_failures_for_retry(tmp_path, monkeypatch):
    (tmp_path / "example.org_urls.txt").write_text("gone\n")
    client = FakeClient({"gone": [NdeUnresolvableUrlError(404, "gone")]})
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path), client=client)
    assert list(tmp_path.glob("example.org_retry_*.txt")) == []


def test_main_passes_retry_to_register_urls(tmp_path, monkeypatch, capsys):
    sleeps = []
    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)
    (tmp_path / "example.org_urls.txt").write_text("flaky\n")
    client = FakeClient({"flaky": [NdeRegisterError(504, "timeout"), NdeValidationResult(True, None)]})
    run_main(monkeypatch, "--skip-oai", "--retry", "1", "--retry-delay", "2.5", "--data-dir", str(tmp_path), client=client)
    assert "Accepted: 1, invalid: 0, failed: 0" in capsys.readouterr().out
    assert len(client.calls_for("flaky")) == 2
    assert sleeps == [2.5]


def test_main_requires_template_without_skip_oai(monkeypatch):
    with pytest.raises(SystemExit):
        run_main(monkeypatch)


def test_main_skip_oai_needs_an_existing_url_file(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path))


def test_main_oai_only_does_not_register(tmp_path, monkeypatch, capsys):
    def fake_retrieve(url, output, **kwargs):
        output.write_text("a\nb\n")

    monkeypatch.setattr(pipeline, "retrieve_identifiers", fake_retrieve)
    client = FakeClient()
    run_main(monkeypatch, "--template", "https://x/{id}", "--oai-only", "--data-dir", str(tmp_path), client=client)
    assert (tmp_path / "example.org_urls.txt").read_text() == "https://x/a\nhttps://x/b\n"
    assert client.calls == []


def test_main_stops_when_no_identifiers_are_found(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "retrieve_identifiers", lambda url, output, **kw: output.write_text(""))
    client = FakeClient()
    run_main(monkeypatch, "--template", "https://x/{id}", "--data-dir", str(tmp_path), client=client)
    assert "No identifiers found" in capsys.readouterr().out
    assert client.calls == []


def test_main_reports_oai_errors(tmp_path, monkeypatch):
    def failing(url, output, **kwargs):
        raise pipeline.OAIError("badArgument", "no such set")

    monkeypatch.setattr(pipeline, "retrieve_identifiers", failing)
    with pytest.raises(SystemExit, match="badArgument"):
        run_main(monkeypatch, "--template", "https://x/{id}", "--data-dir", str(tmp_path))


def test_main_reports_request_errors_during_harvest(tmp_path, monkeypatch):
    def failing(url, output, **kwargs):
        raise pipeline.requests.ConnectionError("refused")

    monkeypatch.setattr(pipeline, "retrieve_identifiers", failing)
    with pytest.raises(SystemExit, match="Harvest failed: ConnectionError"):
        run_main(monkeypatch, "--template", "https://x/{id}", "--data-dir", str(tmp_path))


def test_main_exits_zero_when_all_urls_are_accepted(tmp_path, monkeypatch):
    (tmp_path / "example.org_urls.txt").write_text("a\n")
    run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path))


def test_main_reports_a_disallowed_domain(tmp_path, monkeypatch):
    (tmp_path / "example.org_urls.txt").write_text("a\n")
    client = FakeClient({"a": [NdeDomainNotAllowedError(403, "no")]})
    with pytest.raises(SystemExit, match="allow list"):
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path), client=client)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_write_results_has_a_row_per_url_in_file_order(tmp_path):
    outcomes = {
        "ok": pipeline.UrlOutcome("accepted"),
        "bad": pipeline.UrlOutcome("invalid", "publisher: Add a publisher"),
        "down": pipeline.UrlOutcome("failed", "504: Gateway, Timeout", transient=True),
        "later": pipeline.UrlOutcome(),
    }
    rows = read_csv(pipeline.write_results(tmp_path / "sub" / "r.csv", outcomes))
    assert rows == [
        {"url": "ok", "outcome": "accepted", "detail": ""},
        {"url": "bad", "outcome": "invalid", "detail": "publisher: Add a publisher"},
        {"url": "down", "outcome": "failed", "detail": "504: Gateway, Timeout"},
        {"url": "later", "outcome": "pending", "detail": ""},
    ]


def test_write_results_labels_validated_urls_as_valid(tmp_path):
    outcomes = {"ok": pipeline.UrlOutcome("accepted")}
    rows = read_csv(pipeline.write_results(tmp_path / "r.csv", outcomes, validate_only=True))
    assert rows[0]["outcome"] == "valid"


def test_main_stores_results_in_a_csv(tmp_path, monkeypatch, capsys):
    (tmp_path / "example.org_urls.txt").write_text("good\nbad\n")
    client = FakeClient({"bad": [NdeRegisterError(404, "gone")]})
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path), client=client)
    (results_file,) = tmp_path.glob("example.org_results_*.csv")
    assert [(r["url"], r["outcome"]) for r in read_csv(results_file)] == [("good", "accepted"), ("bad", "failed")]
    assert str(results_file) in capsys.readouterr().out


SHACL = "http://www.w3.org/ns/shacl#"
REPORT = [
    {"@id": "_:b1", "@type": [SHACL + "ValidationReport"]},
    {
        "@id": "_:b2",
        SHACL + "resultSeverity": [{"@id": SHACL + "Violation"}],
        SHACL + "resultPath": [{"@id": "https://schema.org/publisher"}],
        SHACL + "resultMessage": [
            {"@value": "Voeg een uitgever toe", "@language": "nl"},
            {"@value": "Add a publisher", "@language": "en"},
        ],
    },
]


def test_invalid_urls_record_the_violations(tmp_path, monkeypatch):
    (tmp_path / "example.org_urls.txt").write_text("bad\n")
    client = FakeClient({"bad": [NdeValidationResult(False, REPORT)]})
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path), client=client)
    (results_file,) = tmp_path.glob("example.org_results_*.csv")
    assert read_csv(results_file) == [{"url": "bad", "outcome": "invalid", "detail": "publisher: Add a publisher"}]


def test_register_urls_keeps_file_order_and_fills_outcomes(tmp_path):
    client = FakeClient({"b": [NdeRegisterError(504, "timeout"), NdeValidationResult(True, None)]})
    outcomes = {}
    pipeline.register_urls(write_urls(tmp_path, "a", "b", "c"), client, retries=1, retry_delay=0, outcomes=outcomes)
    assert list(outcomes) == ["a", "b", "c"]
    assert {url: o.status for url, o in outcomes.items()} == {"a": "accepted", "b": "accepted", "c": "accepted"}


def test_long_error_bodies_are_shortened_to_one_line(tmp_path):
    body = "<html>\n" + "x " * 5000 + "</html>"
    client = FakeClient({"a": [NdeRegisterError(502, body)]})
    outcomes = {}
    pipeline.register_urls(write_urls(tmp_path, "a"), client, outcomes=outcomes)
    detail = outcomes["a"].detail
    assert len(detail) <= pipeline.MAX_DETAIL
    assert "\n" not in detail
    assert detail.endswith("...")


def test_interrupt_saves_partial_results_and_a_retry_file(tmp_path, monkeypatch, capsys):
    (tmp_path / "example.org_urls.txt").write_text("a\nb\nc\n")
    client = FakeClient({"b": [KeyboardInterrupt()]})
    with pytest.raises(SystemExit, match="Interrupted"):
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path), client=client)
    (results_file,) = tmp_path.glob("example.org_results_*.csv")
    assert [(r["url"], r["outcome"]) for r in read_csv(results_file)] == [("a", "accepted"), ("b", "pending"), ("c", "pending")]
    (retry_file,) = tmp_path.glob("example.org_retry_*.txt")
    assert retry_file.read_text() == "b\nc\n"
    assert "not attempted: 2" in capsys.readouterr().out


def test_disallowed_domain_midway_saves_partial_results(tmp_path, monkeypatch):
    (tmp_path / "example.org_urls.txt").write_text("a\nb\nc\n")
    client = FakeClient({"b": [NdeDomainNotAllowedError(403, "no")]})
    with pytest.raises(SystemExit, match="allow list"):
        run_main(monkeypatch, "--skip-oai", "--data-dir", str(tmp_path), client=client)
    (results_file,) = tmp_path.glob("example.org_results_*.csv")
    assert [(r["url"], r["outcome"]) for r in read_csv(results_file)] == [("a", "accepted"), ("b", "failed"), ("c", "pending")]
    (retry_file,) = tmp_path.glob("example.org_retry_*.txt")
    assert retry_file.read_text() == "c\n"


def test_main_rejects_a_template_without_id(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--template", "https://x/", "--data-dir", str(tmp_path))
