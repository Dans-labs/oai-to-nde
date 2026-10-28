"""Client for the NDE Dataset Register API (https://datasetregister.netwerkdigitaalerfgoed.nl/api/json).
Administrator endpoints are not implemented. 

Hand-written (by IA) rather than generated from the OpenAPI spec: bodies are mostly RDF (Turtle/JSON-LD
SHACL reports) that generators model poorly, and a 400 is treated as a validation result, not an error.
"""
import json
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_BASE_URL = "https://datasetregister.netwerkdigitaalerfgoed.nl/api"
JSON_LD = "application/ld+json"
TURTLE = "text/turtle"
SHACL = "http://www.w3.org/ns/shacl#"


def _local_name(iri: str) -> str:
    return iri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


class NdeRegisterError(Exception):
    """Unexpected response from the Dataset Register."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code


class NdeDomainNotAllowedError(NdeRegisterError):
    """The URL's domain is not on the register's allow list (403)."""


class NdeUnresolvableUrlError(NdeRegisterError):
    """The URL cannot be resolved (404) or contains no datasets (406)."""


@dataclass
class NdeValidationResult:
    # No SHACL violations; warnings still count as valid.
    valid: bool
    # SHACL report as parsed JSON-LD (or raw text for Turtle responses).
    report: Any

    @property
    def violations(self) -> list[str]:
        """The SHACL violations in `report` as 'path: message' (English if available); empty if `report` is not JSON-LD."""
        if not isinstance(self.report, list):
            return []
        found = []
        for node in self.report:
            if not isinstance(node, dict):
                continue
            if {"@id": SHACL + "Violation"} not in node.get(SHACL + "resultSeverity", []):
                continue
            messages = node.get(SHACL + "resultMessage", [])
            english = [m.get("@value", "") for m in messages if m.get("@language") == "en"]
            message = (english or [m.get("@value", "") for m in messages] or [""])[0]
            path = ", ".join(_local_name(p["@id"]) for p in node.get(SHACL + "resultPath", []) if "@id" in p)
            text = f"{path}: {message}" if path and message else path or message
            if text and text not in found:
                found.append(text)
        return found


class NdeDatasetRegisterClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        # token: str | None = None,
        timeout: float = 60.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http = requests.Session()
        # if token:
        #     self._http.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            return self._http.request(method, self._base_url + path, timeout=self._timeout, **kwargs)
        except requests.RequestException as e:
            # The register sometimes drops the connection on slow URLs; report it like any other failure.
            raise NdeRegisterError(0, f"{type(e).__name__}: {e}") from e

    def __enter__(self) -> "NdeDatasetRegisterClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # Onboarding

    def is_domain_allowed(self, url: str) -> bool:
        """Whether the domain of `url` is on the allow list, a precondition for register()."""
        response = self._request("GET", "/allowed-domains", params={"url": url})
        return self._found(response)

    # Validation (read-only)

    def validate_url(self, url: str) -> NdeValidationResult:
        """Validate the dataset description(s) published at `url`."""
        response = self._request(
            "PUT",
            "/datasets/validate",
            json={"@id": url},
            headers={"Content-Type": JSON_LD, "Accept": JSON_LD},
        )
        return self._validation_result(response)

    def validate_body(
        self, description: str | dict | list, content_type: str | None = None
    ) -> NdeValidationResult:
        """Validate a dataset description given inline: Turtle if a str, JSON-LD if a dict/list."""
        if isinstance(description, str):
            content_type = content_type or TURTLE
            content = description.encode()
        else:
            content_type = content_type or JSON_LD
            content = json.dumps(description).encode()
        response = self._request(
            "POST",
            "/datasets/validate",
            data=content,
            headers={"Content-Type": content_type, "Accept": JSON_LD},
        )
        return self._validation_result(response)

    def get_shacl_shape(self, accept: str = JSON_LD) -> str:
        """The SHACL shape graph used for validation."""
        response = self._request("GET", "/shacl", headers={"Accept": accept})
        self._raise_for_status(response)
        return response.text

    # Registration

    def is_registered(self, url: str) -> bool:
        """Whether a registration already exists for `url`."""
        response = self._request("GET", "/datasets", params={"url": url})
        return self._found(response)

    def register(self, url: str) -> NdeValidationResult:
        """Register (or update) the dataset description(s) at `url`.

        Returns valid=True when accepted (202). Invalid descriptions (400) are returned
        with valid=False and the SHACL violations in `report`, and are not registered.
        """
        response = self._request(
            "POST",
            "/datasets",
            json={"@id": url},
            headers={"Content-Type": JSON_LD, "Accept": JSON_LD},
        )
        if response.status_code == 202:
            return NdeValidationResult(True, self._parse(response))
        if response.status_code == 400:
            return NdeValidationResult(False, self._parse(response))
        self._raise_for_status(response)
        raise NdeRegisterError(response.status_code, response.text)

    # Administration (requires a token)

    # def delete_registration(self, url: str) -> None:
    #     response = self._request("DELETE", "/datasets", params={"url": url})
    #     self._raise_for_status(response)

    # def add_allowed_domain(self, domain: str) -> None:
    #     response = self._request("POST", "/allowed-domains", json={"domain": domain})
    #     self._raise_for_status(response)

    # Helpers

    @staticmethod
    def _found(response: requests.Response) -> bool:
        if response.status_code == 404:
            return False
        NdeDatasetRegisterClient._raise_for_status(response)
        return True

    @staticmethod
    def _parse(response: requests.Response) -> Any:
        if JSON_LD in response.headers.get("content-type", ""):
            return response.json()
        return response.text

    @staticmethod
    def _validation_result(response: requests.Response) -> NdeValidationResult:
        if response.status_code in (200, 400):
            return NdeValidationResult(response.status_code == 200, NdeDatasetRegisterClient._parse(response))
        NdeDatasetRegisterClient._raise_for_status(response)
        raise NdeRegisterError(response.status_code, response.text)

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        if response.ok:
            return
        message = response.text
        if response.status_code == 403:
            raise NdeDomainNotAllowedError(response.status_code, message)
        if response.status_code in (404, 406):
            raise NdeUnresolvableUrlError(response.status_code, message)
        raise NdeRegisterError(response.status_code, message)
