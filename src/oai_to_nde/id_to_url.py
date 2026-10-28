# take list of ids from a text file and produce another file with corresponding URLs
from urllib.parse import quote

input_file = "ids.txt"
output_file = "urls.txt"
# {id} is replaced by each ID
url_template = "https://example.com/{id}"

def id_to_url(input_file, output_file, template=url_template):
    """Write one URL per ID (one per line in input_file, blank lines skipped) to output_file.

    Raises ValueError if the template has no {id}, as every URL would then be the same.
    """
    if "{id}" not in template:
        raise ValueError("template must contain {id}")

    with open(input_file, "r", encoding="utf-8") as f:
        identifiers = [line.strip() for line in f if line.strip()]

    with open(output_file, "w", encoding="utf-8") as f:
        for identifier in identifiers:
            # Keep ':' and '/' (valid in a query value, e.g. doi:10.x/y); escape the rest.
            url = template.replace("{id}", quote(identifier, safe=":/"))
            f.write(url + "\n")

# Console-script entry point (id-to-url).
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Convert IDs to URLs")
    parser.add_argument("--input", default=input_file, help="Input file with IDs")
    parser.add_argument("--output", default=output_file, help="Output file with URLs")
    parser.add_argument("--template", default=url_template, help="URL template; {id} is replaced by each ID")
    args = parser.parse_args()
    try:
        id_to_url(args.input, args.output, args.template)
    except ValueError as e:
        parser.error(str(e))

if __name__ == "__main__":
    main()
