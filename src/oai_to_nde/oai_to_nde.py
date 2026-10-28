"""Harvest identifiers from an OAI-PMH endpoint, turn them into URLs and register them in the NDE Dataset Register."""
import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from oai_to_nde.id_to_url import id_to_url
from oai_to_nde.oai_pmh import OAIError, retrieve_identifiers
from oai_to_nde.register_nde_client import NdeDatasetRegisterClient, NdeDomainNotAllowedError, NdeRegisterError

DEFAULT_DATA_DIR = "data"
MAX_DETAIL = 500


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _short(text: str, limit: int = MAX_DETAIL) -> str:
    """One line of at most `limit` characters; error bodies from the register can be whole HTML pages."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _slug(oai_url: str, oai_set: str | None = None, from_date: str | None = None) -> str:
    """Filename-safe prefix for the files of one harvest, e.g. 'example.org_mySet_from-2026-10-01'."""
    name = urlparse(oai_url).netloc
    if oai_set:
        name += f"_{oai_set}"
    if from_date:
        name += f"_from-{from_date}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def data_path(
    oai_url: str, suffix: str, oai_set: str | None = None, data_dir: str = DEFAULT_DATA_DIR, from_date: str | None = None
) -> Path:
    """File of one harvest in data_dir; suffix is e.g. 'ids.txt', 'urls.txt', 'retry_<timestamp>.txt' or 'results_<timestamp>.csv'."""
    return Path(data_dir) / f"{_slug(oai_url, oai_set, from_date)}_{suffix}"


def extract_ids(
    oai_url: str,
    oai_set: str | None = None,
    data_dir: str = DEFAULT_DATA_DIR,
    metadata_prefix: str = "oai_dc",
    from_date: str | None = None,
) -> Path:
    """Harvest the identifiers (optionally of one set, changed since from_date) and store them, one per line, in data_dir."""
    output = data_path(oai_url, "ids.txt", oai_set, data_dir, from_date)
    output.parent.mkdir(parents=True, exist_ok=True)
    retrieve_identifiers(oai_url, output, oai_set=oai_set, metadata_prefix=metadata_prefix, from_date=from_date)
    return output


def generate_urls(ids_file: str | Path, urls_file: str | Path, template: str) -> Path:
    """Write one URL per identifier; `{id}` in the template is replaced by the (escaped) identifier."""
    id_to_url(ids_file, urls_file, template)
    return Path(urls_file)


@dataclass
class UrlOutcome:
    status: str = "pending"  # pending (not attempted yet), accepted, invalid or failed
    detail: str = ""  # the violations of an invalid URL, the error of a failed one
    transient: bool = False  # failed for a reason worth retrying (connection error, 5xx)

    @property
    def retryable(self) -> bool:
        return self.status == "pending" or self.transient


def summarize(outcomes: dict[str, UrlOutcome]) -> dict[str, list]:
    """The URLs per status; failed entries are (url, error) pairs."""
    return {
        "accepted": [url for url, o in outcomes.items() if o.status == "accepted"],
        "invalid": [url for url, o in outcomes.items() if o.status == "invalid"],
        "failed": [(url, o.detail) for url, o in outcomes.items() if o.status == "failed"],
        "pending": [url for url, o in outcomes.items() if o.status == "pending"],
    }


def register_urls(
    urls_file: str | Path,
    client: NdeDatasetRegisterClient,
    validate_only: bool = False,
    retries: int = 0,
    retry_delay: float = 5.0,
    outcomes: dict[str, UrlOutcome] | None = None,
) -> dict[str, list]:
    """Register (or only validate) every URL in the file; returns the URLs per status (see `summarize`).

    Failures that may be transient (dropped connection, 5xx) are retried up to `retries` more times,
    waiting `retry_delay` seconds before each extra pass. `outcomes` (URL to UrlOutcome, in file order)
    is filled in as the run progresses, so a caller can still read it after an interrupt or an error.
    """
    action = client.validate_url if validate_only else client.register
    if outcomes is None:
        outcomes = {}
    with open(urls_file, encoding="utf-8") as f:
        pending = [line.strip() for line in f if line.strip()]
    for url in pending:
        outcomes[url] = UrlOutcome()
    pending = list(outcomes)
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(retry_delay)
        for url in pending:
            try:
                outcome = action(url)
            except NdeDomainNotAllowedError as e:
                # Every URL from one template shares the domain, so stop instead of failing each.
                outcomes[url] = UrlOutcome("failed", _short(str(e)))
                raise
            except NdeRegisterError as e:
                transient = e.status_code == 0 or e.status_code >= 500
                outcomes[url] = UrlOutcome("failed", _short(str(e)), transient)
                continue
            if outcome.valid:
                outcomes[url] = UrlOutcome("accepted")
            else:
                outcomes[url] = UrlOutcome("invalid", _short("; ".join(outcome.violations)))
        pending = [url for url in pending if outcomes[url].transient]
        if not pending:
            break
    return summarize(outcomes)


def write_results(results_file: str | Path, outcomes: dict[str, UrlOutcome], validate_only: bool = False) -> Path:
    """Write one row per URL, in file order: url, status (valid instead of accepted when validating) and detail."""
    results_file = Path(results_file)
    results_file.parent.mkdir(parents=True, exist_ok=True)
    with open(results_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "outcome", "detail"])
        for url, o in outcomes.items():
            status = "valid" if validate_only and o.status == "accepted" else o.status
            writer.writerow([url, status, o.detail])
    return results_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest OAI-PMH identifiers and register them in the NDE Dataset Register")
    parser.add_argument("oai_url", help="OAI-PMH endpoint")
    parser.add_argument("--set", dest="oai_set", help="Only harvest this OAI set")
    parser.add_argument("--metadata-prefix", default="oai_dc")
    parser.add_argument("--from", dest="from_date", help="Only harvest records changed since this date (YYYY-MM-DD or UTC datetime)")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--template", help="URL template; {id} is replaced by each identifier (required unless --skip-oai)")
    parser.add_argument("--skip-oai", action="store_true", help="Skip harvesting and URL generation; register the URL file from an earlier run")
    parser.add_argument("--urls-file", help="Register the URLs in this file (implies --skip-oai), e.g. the *_retry_*.txt of an earlier run")
    parser.add_argument("--oai-only", action="store_true", help="Stop after generating the URL list")
    parser.add_argument("--validate", action="store_true", help="Only validate the URLs against the register, do not register them")
    parser.add_argument("--retry", type=int, default=0, metavar="N", help="Retry URLs that failed with a connection error or 5xx up to N more times")
    parser.add_argument("--retry-delay", type=float, default=5.0, metavar="SECONDS", help="Seconds to wait before each retry pass (default 5)")
    args = parser.parse_args()
    skip_oai = args.skip_oai or args.urls_file is not None
    if not skip_oai and not args.template:
        parser.error("--template is required unless --skip-oai is given")
    if args.template and "{id}" not in args.template:
        parser.error("--template must contain {id}")

    urls_file = Path(args.urls_file) if args.urls_file else data_path(args.oai_url, "urls.txt", args.oai_set, args.data_dir, args.from_date)
    if skip_oai:
        if not urls_file.exists():
            parser.error(f"no URL file from an earlier run: {urls_file}")
    else:
        try:
            print(f"Retrieving identifiers from {args.oai_url} ...")
            ids_file = extract_ids(args.oai_url, args.oai_set, args.data_dir, args.metadata_prefix, args.from_date)
        except OAIError as e:
            sys.exit(f"OAI-PMH error: {e}")
        except requests.RequestException as e:
            sys.exit(f"Harvest failed: {type(e).__name__}: {e}")
        except KeyboardInterrupt:
            sys.exit("Interrupted during harvest.")
        print(f"Identifiers: {ids_file}")
        if ids_file.stat().st_size == 0:
            print("No identifiers found; nothing to register.")
            return
        print("Done retrieving identifiers.")
        print(f"Generating URLs using template: {args.template} ...")
        generate_urls(ids_file, urls_file, args.template)
        print("Done generating URLs.")
    print(f"URLs: {urls_file}")
    if args.oai_only:
        return

    stamp = _timestamp()
    outcomes: dict[str, UrlOutcome] = {}
    stop = None
    with NdeDatasetRegisterClient() as client:
        try:
            print(f"Registering or validating URLs from {urls_file} ...")
            register_urls(urls_file, client, validate_only=args.validate, retries=args.retry, retry_delay=args.retry_delay, outcomes=outcomes)
        except NdeDomainNotAllowedError:
            stop = "Error: the domain of the URLs is not on the register's allow list; ask NDE to add it."
        except KeyboardInterrupt:
            stop = "Interrupted; the results so far were saved."
        print("Done registering or validating URLs.")

    # Save before reporting, so an interrupted or aborted run keeps what it has.
    results_file_name = f"results_{stamp}.csv"
    print(f"Writing results to file {results_file_name} ...")
    results_file = write_results(
        data_path(args.oai_url, results_file_name, args.oai_set, args.data_dir, args.from_date),
        outcomes,
        validate_only=args.validate,
    )
    retry_urls = [url for url, o in outcomes.items() if o.retryable]
    retry_file = None
    if retry_urls:
        retry_file_name = f"retry_{stamp}.txt"
        print(f"Writing retry URLs to file {retry_file_name} ...")
        retry_file = data_path(args.oai_url, retry_file_name, args.oai_set, args.data_dir, args.from_date)
        retry_file.write_text("".join(f"{url}\n" for url in retry_urls), encoding="utf-8")
        print("Done writing retry URLs.")
    result = summarize(outcomes)
    ok = "Valid" if args.validate else "Accepted"
    summary = f"{ok}: {len(result['accepted'])}, invalid: {len(result['invalid'])}, failed: {len(result['failed'])}"
    if result["pending"]:
        summary += f", not attempted: {len(result['pending'])}"
    print(summary)
    print(f"Results: {results_file}")
    for url in result["invalid"]:
        detail = outcomes[url].detail
        print(f"  invalid: {url}" + (f" ({detail})" if detail else ""))
    for url, error in result["failed"]:
        print(f"  failed: {url} ({error})")
    if retry_file:
        print(f"URLs to try again saved to {retry_file}; retry with --urls-file")
    if stop:
        sys.exit(stop)
    if result["invalid"] or result["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
