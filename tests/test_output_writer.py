"""Tests for Excel output writer."""
import os, tempfile, pytest, openpyxl, sys
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from output_writer import OutputWriter, FILL_LIGHT_RED, FILL_GREY

@pytest.fixture
def sample_results():
    return [
        {'ticker': 'CGT', 'transaction_type': 'Buyback', 'average_price': 5028.63,
         'shares_transacted': 25981, 'announcement_date': '16 March 2026',
         'effective_date': '16 March 2026', 'voting_rights': 15747163, 'page_text': 'CGT text...'},
        {'ticker': 'BRGE', 'transaction_type': 'Buyback', 'average_price': 546.53,
         'shares_transacted': 25000, 'announcement_date': '16 March 2026',
         'effective_date': '18 March 2026', 'voting_rights': 92112641, 'page_text': 'BRGE text...'},
    ]

class TestOutputFormat:
    def test_column_headers(self, sample_results):
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(sample_results, path)
            wb = openpyxl.load_workbook(path)
            ws = wb['output']
            headers = [ws.cell(row=1, column=i).value for i in range(1, 12)]
            assert headers == ['Ticker', 'Event Type', 'Price', 'Cancelled shares',
                             'Treasury shares', 'Issued Shares', 'Announce date',
                             'Transaction date', 'Description', 'New shares in issue', 'Announcement']
        finally:
            os.unlink(path)

    def test_ticker_has_ln_suffix(self, sample_results):
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(sample_results, path)
            wb = openpyxl.load_workbook(path)
            ws = wb['output']
            assert ws.cell(row=2, column=1).value == 'CGT LN'
            assert ws.cell(row=3, column=1).value == 'BRGE LN'
        finally:
            os.unlink(path)

    def test_buyback_shares_in_treasury_column(self, sample_results):
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(sample_results, path)
            wb = openpyxl.load_workbook(path)
            ws = wb['output']
            assert ws.cell(row=2, column=5).value == 25981  # Treasury shares
            assert ws.cell(row=2, column=6).value is None    # Issued Shares blank
        finally:
            os.unlink(path)

    def test_date_format(self, sample_results):
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(sample_results, path)
            wb = openpyxl.load_workbook(path)
            ws = wb['output']
            cell = ws.cell(row=2, column=7)
            assert isinstance(cell.value, datetime), f"Expected datetime, got {type(cell.value)}: {cell.value}"
            assert cell.value.year == 2026
            assert cell.value.month == 3
            assert cell.value.day == 16
            assert cell.number_format == 'DD-MMM-YYYY'
        finally:
            os.unlink(path)

    def test_sheet_name(self, sample_results):
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(sample_results, path)
            wb = openpyxl.load_workbook(path)
            assert 'output' in wb.sheetnames
        finally:
            os.unlink(path)


class TestBlankFieldHighlighting:
    def test_blank_price_highlighted(self):
        results = [{'ticker': 'XTST', 'transaction_type': 'Buyback', 'average_price': None,
                     'shares_transacted': 1000, 'announcement_date': '23 March 2026',
                     'effective_date': '23 March 2026', 'voting_rights': 100000, 'page_text': '...'}]
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(results, path)
            wb = openpyxl.load_workbook(path)
            ws = wb['output']
            price_cell = ws.cell(row=2, column=3)  # C = Price
            assert price_cell.fill == FILL_LIGHT_RED
        finally:
            os.unlink(path)

    def test_blank_shares_in_issue_highlighted(self):
        results = [{'ticker': 'YTST', 'transaction_type': 'Buyback', 'average_price': 100.0,
                     'shares_transacted': 500, 'announcement_date': '23 March 2026',
                     'effective_date': '23 March 2026', 'voting_rights': None,
                     'shares_outstanding': None, 'page_text': '...'}]
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(results, path)
            wb = openpyxl.load_workbook(path)
            ws = wb['output']
            sii_cell = ws.cell(row=2, column=10)  # J = New shares in issue
            assert sii_cell.fill == FILL_LIGHT_RED
        finally:
            os.unlink(path)

    def test_filled_price_not_highlighted(self, sample_results):
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(sample_results, path)
            wb = openpyxl.load_workbook(path)
            ws = wb['output']
            price_cell = ws.cell(row=2, column=3)  # C = Price (has value 5028.63)
            assert price_cell.fill != FILL_LIGHT_RED
        finally:
            os.unlink(path)


class TestNotTrackedSheet:
    def test_not_tracked_on_separate_sheet(self, monkeypatch):
        # The public repo doesn't ship a not_tracked_tickers.xlsx, so the runtime
        # NOT_TRACKED_TICKERS list is empty. Patch it for this test so we can
        # exercise the "Not Tracked" sheet logic.
        import config
        monkeypatch.setattr(config, 'NOT_TRACKED_TICKERS', ['AJB', 'LAND'])
        results = [
            {'ticker': 'CGT', 'transaction_type': 'Buyback', 'average_price': 5000.0,
             'shares_transacted': 100, 'announcement_date': '23 March 2026',
             'effective_date': '23 March 2026', 'voting_rights': 1000000, 'page_text': '...'},
            {'ticker': 'AJB', 'transaction_type': 'Buyback', 'average_price': 200.0,
             'shares_transacted': 50, 'announcement_date': '23 March 2026',
             'effective_date': '23 March 2026', 'voting_rights': 500000, 'page_text': '...'},
            {'ticker': 'LAND', 'transaction_type': 'Issuance', 'average_price': 150.0,
             'shares_transacted': 75, 'announcement_date': '23 March 2026',
             'effective_date': '23 March 2026', 'voting_rights': 300000, 'page_text': '...'},
        ]
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(results, path)
            wb = openpyxl.load_workbook(path)

            # Main sheet should only have CGT
            ws = wb['output']
            assert ws.cell(row=2, column=1).value == 'CGT LN'
            assert ws.max_row == 2  # header + 1 data row

            # Not Tracked sheet should have AJB and LAND
            ws_nt = wb['Not Tracked']
            tickers = [ws_nt.cell(row=r, column=1).value for r in range(2, ws_nt.max_row + 1)]
            assert 'AJB LN' in tickers
            assert 'LAND LN' in tickers
            assert len(tickers) == 2

            # Grey fill applied to not-tracked rows
            assert ws_nt.cell(row=2, column=1).fill == FILL_GREY
        finally:
            os.unlink(path)

    def test_no_not_tracked_sheet_when_all_tracked(self, sample_results):
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as f:
            path = f.name
        try:
            OutputWriter.write(sample_results, path)
            wb = openpyxl.load_workbook(path)
            assert 'Not Tracked' not in wb.sheetnames
        finally:
            os.unlink(path)
