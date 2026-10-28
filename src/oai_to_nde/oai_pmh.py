# Harvesting client code for OAI-PMH (Open Archives Initiative Protocol for Metadata Harvesting) 

import requests
import urllib3
import xml.etree.ElementTree as ET

OAI_NAMESPACE = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_NAMESPACE = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"

ET.register_namespace("oai", OAI_NAMESPACE)
ET.register_namespace("oai_dc", OAI_DC_NAMESPACE)
ET.register_namespace("dc", DC_NAMESPACE)
ET.register_namespace("xsi", XSI_NAMESPACE)

HEADER_TAG = f"{{{OAI_NAMESPACE}}}header"
IDENTIFIER_TAG = f"{{{OAI_NAMESPACE}}}identifier"
RESUMPTION_TOKEN_TAG = f"{{{OAI_NAMESPACE}}}resumptionToken"
ERROR_TAG = f"{{{OAI_NAMESPACE}}}error"


class OAIError(Exception):
    """The server answered with an OAI-PMH <error> element."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class OAIClient:
    def __init__(self, base_url, timeout=60):
        self.base_url = base_url
        self.timeout = timeout

    def request(self, params):
        response = requests.get(self.base_url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return ET.fromstring(response.content)
    
    def identify(self):
        return self.request({'verb': 'Identify'})
    
    def list_metadata_formats(self):
        return self.request({'verb': 'ListMetadataFormats'})

    def list_sets(self):
        return self.request({'verb': 'ListSets'})
    
    def list_identifiers(self, metadata_prefix='oai_dc', oai_set=None, **kwargs):
        params = {'verb': 'ListIdentifiers', 'metadataPrefix': metadata_prefix}
        if oai_set:
            params['set'] = oai_set
        params.update(kwargs)
        return self.request(params)

    def iter_identifier_pages(self, metadata_prefix='oai_dc', from_date=None, oai_set=None, **kwargs):
        """Yield one list of identifiers for each server-defined OAI-PMH page."""
        params = {'metadataPrefix': metadata_prefix, **kwargs}
        if from_date:
            params['from'] = from_date
        if oai_set:
            params['set'] = oai_set

        while True:
            response = requests.get(self.base_url, params={'verb': 'ListIdentifiers', **params}, stream=True, timeout=self.timeout)
            response.raise_for_status()
            response.raw.decode_content = True
            identifiers = []
            token = None

            # Parse the XML response for headers and resumption token.
            # Note that while the total number of records may be large, each 'page' will not be huge
            try:
                for _, element in ET.iterparse(response.raw, events=('end',)):
                    if element.tag == HEADER_TAG:
                        # skip if it has status="deleted"
                        # maybe we want to store that also somewhere, so we can get rid of these records at some point
                        if element.attrib.get('status') == 'deleted':
                            element.clear()
                            continue
                        identifier = element.find(IDENTIFIER_TAG)
                        if identifier is not None and identifier.text:
                            identifiers.append(identifier.text)
                        element.clear()
                    elif element.tag == RESUMPTION_TOKEN_TAG:
                        token = (element.text or '').strip()
                        element.clear()
                    elif element.tag == ERROR_TAG:
                        # An empty result is not a failure; anything else (bad set, bad prefix, ...) is.
                        if element.attrib.get('code') != 'noRecordsMatch':
                            raise OAIError(element.attrib.get('code'), (element.text or '').strip())
                if identifiers:
                    yield identifiers
            except ET.ParseError as exc:
                raise OAIError('invalidXml', str(exc)) from exc
            except urllib3.exceptions.HTTPError as exc:
                raise requests.exceptions.ChunkedEncodingError(str(exc)) from exc
            finally:
                response.close()

            if not token:
                break
            params = {'resumptionToken': token}

    # An iterator to get all identifiers using the resumption token.
    # Example usage:
    # client = OAIClient("https://example.org/oai")
    # for identifier in client.iter_identifiers():
    #     print(identifier)
    def iter_identifiers(self, metadata_prefix='oai_dc', from_date=None, oai_set=None, **kwargs):
        for identifiers in self.iter_identifier_pages(metadata_prefix, from_date=from_date, oai_set=oai_set, **kwargs):
            yield from identifiers



def retrieve_identifiers(url, output_file, oai_set=None, metadata_prefix='oai_dc', from_date=None):
    client = OAIClient(url)
    with open(output_file, "w", encoding="utf-8") as file_handle:
        for identifier in client.iter_identifiers(metadata_prefix=metadata_prefix, from_date=from_date, oai_set=oai_set):
            file_handle.write(identifier + "\n")
            # maybe log something in a file or database record?
            #console.print(f"Retrieved identifier: {identifier}", markup=False)


def get_save_dir_or_file_name(name):
    return name.replace(':', '_').replace('/', '_')

# simple cli for retrieving the identifiers from an endpoint with an optional set and from date
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Retrieve OAI-PMH identifiers")
    parser.add_argument("oai_url", help="OAI-PMH endpoint")
    parser.add_argument("--set", dest="oai_set", help="Only harvest this OAI set")
    parser.add_argument("--metadata-prefix", default="oai_dc")
    parser.add_argument("--from-date", dest="from_date", help="Only harvest records from this date (YYYY-MM-DD)")
    parser.add_argument("--output-file", required=True, help="File to store the retrieved identifiers")
    args = parser.parse_args()

    retrieve_identifiers(args.oai_url, args.output_file, oai_set=args.oai_set, metadata_prefix=args.metadata_prefix, from_date=args.from_date)


if __name__ == "__main__":
    main()
