oai-to-nde
==========

Register the datasets of an OAI-PMH repository in the
[NDE Dataset Register](https://datasetregister.netwerkdigitaalerfgoed.nl/).

A run consists of three steps:

1. Extract the identifiers from the OAI-PMH endpoint.
2. Generate a URL for each identifier from a template.
3. Register the URLs with the Dataset Register, or only validate them.

The intermediate lists are stored in a data directory (`data/` by default), so a later run can pick up where an earlier one stopped.

# Requirements

* Python 3.11+ and [uv](https://docs.astral.sh/uv/)
* To register (not to validate), the domain of the generated URLs must be on the register's allow list. [Contact NDE](mailto:bob.coret@netwerkdigitaalerfgoed.nl) to have it added.

```bash
uv sync
```

# Usage

```bash
uv run oai-to-nde <oai_url> --template <url_template> [options]
```

| Option | Description |
|---|---|
| `--template` | URL template, `{id}` is replaced by each URL-escaped identifier (`:` and `/` are kept, as in `doi:10.x/y`). Must contain `{id}`; required unless `--skip-oai`. |
| `--set` | Only harvest this OAI set. |
| `--metadata-prefix` | OAI metadata prefix, default `oai_dc`. |
| `--from` | Only harvest records changed since this date (`YYYY-MM-DD` or UTC datetime). The date becomes part of the file names. |
| `--data-dir` | Where the identifier and URL lists are stored, default `data`. |
| `--oai-only` | Stop after generating the URL list; nothing is sent to the register. |
| `--validate` | Validate the URLs against the register instead of registering them. |
| `--retry N` | Retry URLs that failed with a connection error or a 5xx response up to N more times. Other failures (404, 406) are not retried. |
| `--retry-delay SECONDS` | Seconds to wait before each retry pass, default `5`. |
| `--skip-oai` | Skip harvesting and URL generation, use the URL file of an earlier run. |
| `--urls-file` | Register the URLs in this file instead (implies `--skip-oai`), e.g. the retry file of an earlier run. |

Files are named after the endpoint host, the set and the `--from` date: `<data-dir>/<host>[_<set>][_from-<date>]_ids.txt` and `<data-dir>/<host>[_<set>][_from-<date>]_urls.txt`.
With `--skip-oai`, pass the same `oai_url`, `--set`, `--from` and `--data-dir` as in the earlier run.

Invalid datasets and failed requests are listed at the end of a run, followed by a summary line with the counts.

Every run also stores one row per URL, in the order of the URL file, in `<data-dir>/<host>[_<set>][_from-<date>]_results_<timestamp>.csv`. The timestamp is the local time the registration started, e.g. `20261027T101500`.

| Column | Description |
|---|---|
| `url` | The URL that was registered or validated. |
| `outcome` | `accepted` (`valid` with `--validate`), `invalid`, `failed` or `pending` (not attempted, because the run stopped early). |
| `detail` | For `invalid`, the SHACL violations as `path: message`; for `failed`, the error. Shortened to 500 characters. |

URLs worth trying again (failed with a connection error or a 5xx response, or still pending) are saved next to it in `<data-dir>/<host>[_<set>][_from-<date>]_retry_<timestamp>.txt`. Register just those with `--urls-file`. URLs that failed for good, such as a 404 or 406, are not in that file.

The results and the retry file are also written when the run is interrupted with Ctrl-C or stops because the domain is not on the allow list, so nothing already done is lost.
An error reply from the OAI-PMH server, such as an unknown set, stops the run with its message, as do network errors and malformed XML from the endpoint (`Harvest failed: ...`, `invalidXml`).

The exit code is 0 when all URLs were accepted (or valid), and 1 when any URL is invalid or failed, when the run was interrupted, or when the domain is not allowed. This makes the command usable in cron jobs and CI.

# Examples

Extract the 'humanities' datasets from the SSH datastation and register them at NDE:

```bash
uv run oai-to-nde https://ssh.datastations.nl/oai --set humanities \
  --template "https://ssh.datastations.nl/api/datasets/export?exporter=dcat3-turtle&persistentId={id}"
```

Do a dry run: harvest, generate the URLs and only validate them:

```bash
uv run oai-to-nde https://ssh.datastations.nl/oai --set humanities --validate \
  --template "https://ssh.datastations.nl/api/datasets/export?exporter=dcat3-turtle&persistentId={id}"
```

Register the URLs of that earlier harvest without harvesting again:

```bash
uv run oai-to-nde https://ssh.datastations.nl/oai --set humanities --skip-oai
```

## Separate steps

Generate the list of URLs from a list of identifiers (one per line, blank lines are skipped) extracted earlier:

```bash
uv run id-to-url --input ./data/clarin.txt --output ./data/nde.txt \
  --template "https://ssh.datastations.nl/api/datasets/export?exporter=dcat3-turtle&persistentId={id}"
```

This is rarely needed, only when doing the work in separate steps.
Without options, `id-to-url` reads `ids.txt`, writes `urls.txt` and uses the template `https://example.com/{id}`; the template must contain `{id}`.
The OAI-PMH harvest is also available on its own, without URL generation or registration (note that here the date option is `--from-date`, in `YYYY-MM-DD` format):

```bash
uv run oai-get-ids https://ssh.datastations.nl/oai --set humanities --output-file ./data/humanities.txt
```

# Use as a library

```python
from oai_to_nde.oai_to_nde import extract_ids, generate_urls, register_urls, data_path
from oai_to_nde.register_nde_client import NdeDatasetRegisterClient

oai_url = "https://ssh.datastations.nl/oai"
ids_file = extract_ids(oai_url, oai_set="humanities")
urls_file = generate_urls(ids_file, data_path(oai_url, "urls.txt", "humanities"), "https://example.org/export?id={id}")

with NdeDatasetRegisterClient() as client:
    result = register_urls(urls_file, client, retries=2, retry_delay=5)
# result = {"accepted": [...], "invalid": [...], "failed": [(url, error), ...], "pending": [...]}
```

`register_urls` takes these optional arguments:

| Argument | Description |
|---|---|
| `validate_only` | Validate instead of register, default `False`. |
| `retries` | Extra passes over URLs that failed with a connection error or a 5xx response, default `0`. |
| `retry_delay` | Seconds to wait before each extra pass, default `5`. |
| `outcomes` | A dict that is filled with a `UrlOutcome` per URL, in file order, as the run progresses; it stays readable after an interrupt or an error. |

`extract_ids` raises `oai_to_nde.oai_pmh.OAIError` when the OAI-PMH server answers with an error, and `register_urls` raises `NdeDomainNotAllowedError` when the domain is not on the allow list.
When using `--from`, pass the same `from_date` to `data_path` to get the file names the command line would use.

`NdeDatasetRegisterClient` also offers `is_registered`, `is_domain_allowed`, `validate_url`, `validate_body` and `get_shacl_shape`.
It is hand-written rather than generated from the register's [OpenAPI spec](https://datasetregister.netwerkdigitaalerfgoed.nl/api/json).

# Tests

```bash
uv run pytest
```

The tests use fake OAI-PMH and register responses, so they need no network access.

# Notes

The harvesting code in `oai_pmh.py` started from the code in the GitHub repo [Dans-labs/harvesting-docs](https://github.com/Dans-labs/harvesting-docs).

