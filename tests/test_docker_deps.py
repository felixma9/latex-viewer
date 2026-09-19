from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_installs_poppler():
    """/page/ shells out to pdftoppm and pdfinfo; both ship in poppler-utils."""
    assert "poppler-utils" in (ROOT / "Dockerfile").read_text()


def test_readme_documents_the_mobile_route():
    readme = (ROOT / "README.md").read_text()
    assert "/m" in readme
