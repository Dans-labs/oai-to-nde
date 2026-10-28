import pytest

from oai_to_nde.id_to_url import id_to_url


def convert(tmp_path, ids, template):
    source = tmp_path / "ids.txt"
    target = tmp_path / "urls.txt"
    source.write_text("".join(f"{i}\n" for i in ids))
    id_to_url(source, target, template)
    return target.read_text().splitlines()


def test_substitutes_id_in_template(tmp_path):
    urls = convert(tmp_path, ["a", "b"], "https://x/{id}")
    assert urls == ["https://x/a", "https://x/b"]


def test_keeps_doi_characters_unescaped(tmp_path):
    urls = convert(tmp_path, ["doi:10.17026/DANS-222-ZET2"], "https://x/?persistentId={id}")
    assert urls == ["https://x/?persistentId=doi:10.17026/DANS-222-ZET2"]


def test_escapes_url_special_characters(tmp_path):
    urls = convert(tmp_path, ["a&b#c d"], "https://x/?id={id}")
    assert urls == ["https://x/?id=a%26b%23c%20d"]


def test_other_braces_in_template_are_left_alone(tmp_path):
    urls = convert(tmp_path, ["a"], "https://x/{foo}?id={id}")
    assert urls == ["https://x/{foo}?id=a"]


def test_blank_lines_are_skipped(tmp_path):
    source = tmp_path / "ids.txt"
    target = tmp_path / "urls.txt"
    source.write_text("a\n\n  \nb\n")
    id_to_url(source, target, "https://x/{id}")
    assert target.read_text().splitlines() == ["https://x/a", "https://x/b"]


def test_template_without_id_is_rejected(tmp_path):
    source = tmp_path / "ids.txt"
    source.write_text("a\n")
    with pytest.raises(ValueError, match=r"\{id\}"):
        id_to_url(source, tmp_path / "urls.txt", "https://x/")
