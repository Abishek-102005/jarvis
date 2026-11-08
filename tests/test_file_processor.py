import tempfile
import unittest
from pathlib import Path

from actions.file_processor import _detect_type, _extract_text_from_file


class FileProcessorReadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _write(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_detects_common_document_types(self):
        expected = {
            "report.pdf": "pdf",
            "notes.docx": "docx",
            "data.xlsx": "excel",
            "slides.pptx": "pptx",
            "page.html": "code",
            "data.json": "json",
            "data.tsv": "csv",
            "source.py": "code",
        }
        for filename, file_type in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(_detect_type(Path(filename)), file_type)

    def test_reads_utf8_text_and_respects_character_limit(self):
        path = self._write("notes.txt", "\ufeffHello, JARVIS!")
        self.assertEqual(_extract_text_from_file(path), "Hello, JARVIS!")
        self.assertEqual(_extract_text_from_file(path, max_chars=5), "Hello")

    def test_reads_json_as_formatted_text(self):
        path = self._write("data.json", '{"name":"JARVIS","ready":true}')
        result = _extract_text_from_file(path)
        self.assertIn('"name": "JARVIS"', result)
        self.assertIn('"ready": true', result)

    def test_reads_delimited_rows(self):
        path = self._write("data.tsv", "name\trole\nJARVIS\tassistant\n")
        self.assertEqual(_extract_text_from_file(path), "name\trole\nJARVIS\tassistant")

    def test_reads_html_without_markup(self):
        path = self._write("page.html", "<html><title>Title</title><body>Hello <b>world</b></body></html>")
        self.assertIn("Hello\nworld", _extract_text_from_file(path))

    def test_reads_xml_text(self):
        path = self._write("data.xml", "<root><item>Alpha</item><item>Beta</item></root>")
        self.assertEqual(_extract_text_from_file(path), "Alpha\nBeta")

    def test_reads_word_document_paragraphs_and_tables(self):
        from docx import Document

        path = self.root / "report.docx"
        document = Document()
        document.add_paragraph("Quarterly report")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Revenue"
        table.cell(0, 1).text = "42"
        document.save(path)

        result = _extract_text_from_file(path)
        self.assertIn("Quarterly report", result)
        self.assertIn("Revenue\t42", result)

    def test_reads_excel_sheet_cells(self):
        import pandas as pd

        path = self.root / "data.xlsx"
        pd.DataFrame([["Name", "Role"], ["JARVIS", "assistant"]]).to_excel(
            path, index=False, header=False
        )
        result = _extract_text_from_file(path)
        self.assertIn("JARVIS", result)
        self.assertIn("assistant", result)

    def test_reads_powerpoint_slide_text(self):
        from pptx import Presentation
        from pptx.util import Inches

        path = self.root / "slides.pptx"
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text = "Project overview"
        presentation.save(path)

        self.assertIn("Project overview", _extract_text_from_file(path))

    def test_handles_pdf_without_selectable_text(self):
        from pypdf import PdfWriter

        path = self.root / "scanned.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        with path.open("wb") as stream:
            writer.write(stream)

        self.assertIn("No selectable text found", _extract_text_from_file(path))

    def test_unknown_text_format_is_read_as_text(self):
        path = self._write("settings.custom", "setting = enabled")
        self.assertEqual(_extract_text_from_file(path), "setting = enabled")

    def test_unknown_binary_format_returns_clear_error(self):
        path = self.root / "unknown.bin"
        path.write_bytes(b"\x00\x01\x02")
        with self.assertRaisesRegex(RuntimeError, "Unsupported binary file format"):
            _extract_text_from_file(path)


if __name__ == "__main__":
    unittest.main()
