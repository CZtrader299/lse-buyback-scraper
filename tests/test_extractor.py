"""Tests for regex extraction logic."""
import os, sys, pytest
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extractor import Extractor

@pytest.fixture
def extractor():
    return Extractor()

@pytest.fixture
def cgt_text():
    path = os.path.join(os.path.dirname(__file__), 'fixtures', 'sample_announcement_cgt.txt')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

@pytest.fixture
def brge_text():
    path = os.path.join(os.path.dirname(__file__), 'fixtures', 'sample_announcement_brge.txt')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

class TestExtractFields:
    def test_cgt_shares_transacted(self, extractor, cgt_text):
        result = extractor.extract(cgt_text, 'CGT')
        assert result['shares_transacted'] == 25981

    def test_cgt_price(self, extractor, cgt_text):
        result = extractor.extract(cgt_text, 'CGT')
        assert result['average_price'] == 5028.63

    @pytest.mark.parametrize(
        "ticker,text,expected",
        [
            (
                "SMT",
                "The Company announces the issuance of 1,300,000 shares "
                "at a price of 1,426.00p, each fully paid from Treasury.",
                1426.0,
            ),
            (
                "BIPS",
                "The Company has agreed today to issue and allot 150,000 ordinary "
                "shares at a price of 171.59p per share.",
                171.59,
            ),
            (
                "HAN",
                "The Company purchased 90,000 ordinary A non-voting shares "
                "at a price of 279.00p. These shares will be cancelled.",
                279.0,
            ),
            (
                "BSRT",
                "The Company purchased for cancellation 12,900 ordinary shares "
                "at a price of 127.35p per share.",
                127.35,
            ),
            (
                "VNH",
                "The Company purchased 44,246 Ordinary Shares at an average "
                "price of 363 pence per Ordinary Share.",
                363.0,
            ),
        ],
    )
    def test_pence_prices_keep_decimal_place(self, extractor, ticker, text, expected):
        result = extractor.extract(text, ticker)
        assert result['average_price'] == expected

    def test_average_price_preferred_over_highest_and_lowest(self, extractor):
        text = (
            "Date of purchase: 1 May 2026. Number of Shares purchased: 624,500 Shares. "
            "Highest price paid per Share: 105.68 pence. "
            "Lowest price paid per Share: 104.50 pence. "
            "Average price paid per Share: 105.48 pence. "
            "Total number of Shares in issue excluding treasury: 103,721,042."
        )
        result = extractor.extract(text, 'GPM')
        assert result['average_price'] == 105.48

    def test_gbp_price_converted_to_pence(self, extractor):
        # VOF "GBP X.XX per share → pence" conversion relies on a ticker-specific
        # pattern from the proprietary patterns DB (not shipped with the public repo).
        if extractor.patterns_db is None:
            pytest.skip("requires ticker patterns DB (not shipped in public repo)")
        text = (
            "The Company repurchased 37,056 Ordinary Shares at a price of "
            "GBP 4.614980 per share. These shares will be held in treasury."
        )
        result = extractor.extract(text, 'VOF')
        assert result['average_price'] == 461.498

    def test_cgt_shares_in_issue(self, extractor, cgt_text):
        # CGT's "shares in issue" label appears in a format that needs a
        # ticker-specific pattern from the proprietary patterns DB.
        if extractor.patterns_db is None:
            pytest.skip("requires ticker patterns DB (not shipped in public repo)")
        result = extractor.extract(cgt_text, 'CGT')
        assert result['voting_rights'] == 15747163

    def test_cgt_transaction_type(self, extractor, cgt_text):
        result = extractor.extract(cgt_text, 'CGT')
        assert result['transaction_type'] == 'Buyback'

    def test_brge_effective_date_differs(self, extractor, brge_text):
        result = extractor.extract(brge_text, 'BRGE')
        assert result['effective_date'] is not None
        assert result['effective_date'] != result['announcement_date']

    def test_with_effect_from_date(self, extractor):
        """'With effect from DD Month YYYY' should be extracted as effective date."""
        text = (
            "BRAI announces it has purchased 50,000 ordinary shares. "
            "With effect from 16 April 2026, the total voting rights will be 10,000,000. "
            "Number of shares purchased: 50,000. Average price: 120.00p. "
            "Announcement date: 17 April 2026."
        )
        result = extractor.extract(text, 'BRAI')
        assert result['effective_date'] is not None
        # Should resolve to 16 April 2026 (_normalize_date returns 'DD Month YYYY')
        assert result['effective_date'] == '16 April 2026'

class TestNormalizeDate:
    def test_dd_month_yyyy(self, extractor):
        assert extractor.normalize_date_for_output('16 March 2026') == '2026-03-16 00:00:00'

    def test_dd_mm_yyyy(self, extractor):
        assert extractor.normalize_date_for_output('16/03/2026') == '2026-03-16 00:00:00'

    def test_yyyy_mm_dd(self, extractor):
        assert extractor.normalize_date_for_output('2026-03-16') == '2026-03-16 00:00:00'

    def test_single_digit_day(self, extractor):
        assert extractor.normalize_date_for_output('5 March 2026') == '2026-03-05 00:00:00'

class TestParseDateToDatetime:
    def test_dd_month_yyyy(self):
        result = Extractor.parse_date_to_datetime('16 March 2026')
        assert isinstance(result, datetime)
        assert result == datetime(2026, 3, 16)

    def test_dd_mm_yyyy_slash(self):
        result = Extractor.parse_date_to_datetime('16/03/2026')
        assert isinstance(result, datetime)
        assert result == datetime(2026, 3, 16)

    def test_yyyy_mm_dd(self):
        result = Extractor.parse_date_to_datetime('2026-03-16')
        assert isinstance(result, datetime)
        assert result == datetime(2026, 3, 16)

    def test_yyyy_mm_dd_with_time(self):
        result = Extractor.parse_date_to_datetime('2026-03-16 00:00:00')
        assert isinstance(result, datetime)
        assert result == datetime(2026, 3, 16)

    def test_none_returns_none(self):
        assert Extractor.parse_date_to_datetime(None) is None

    def test_empty_string_returns_none(self):
        assert Extractor.parse_date_to_datetime('') is None

    def test_already_datetime_passes_through(self):
        dt = datetime(2026, 3, 16)
        assert Extractor.parse_date_to_datetime(dt) is dt


class TestEffectiveDateRange:
    """Test date range extraction for announcements like VOF."""

    def test_period_from_to(self, extractor):
        text = ("23 March 2026 VinaCapital Vietnam Opportunity Fund announces "
                "the following transactions carried out during the period from "
                "17 March 2026 to 20 March 2026. "
                "50,000 ordinary shares were purchased at 500p per share. "
                "Total voting rights: 1,000,000.")
        result = extractor.extract(text, 'VOF')
        assert result['effective_date'] == '20 March 2026'

    def test_between_and(self, extractor):
        text = ("23 March 2026 XYZ Fund announces transactions between "
                "17 March 2026 and 21 March 2026. "
                "Purchased 10,000 ordinary shares at 200p. "
                "Total voting rights: 500,000.")
        result = extractor.extract(text, 'XYZ')
        assert result['effective_date'] == '21 March 2026'

    def test_from_to_with_dd_mm_yyyy(self, extractor):
        text = ("23/03/2026 Fund ABC transactions from 17/03/2026 to 20/03/2026. "
                "Purchased 5,000 ordinary shares at 100p. Voting rights: 200,000.")
        result = extractor.extract(text, 'ABC')
        assert result['effective_date'] is not None
        assert '20' in result['effective_date']

    def test_single_date_still_works(self, extractor):
        """Existing single-date patterns still work when no range is present."""
        text = ("23 March 2026 XYZ announces that on 20 March 2026 it purchased "
                "10,000 ordinary shares at 500p per share. Voting rights: 1,000,000.")
        result = extractor.extract(text, 'XYZ')
        assert result['effective_date'] == '20 March 2026'


class TestDealingsCommenceEffectiveDate:
    """Effective date derived from 'dealings ... commence on' for issuance announcements."""

    def test_dealings_expected_to_commence(self, extractor):
        """'dealings are expected to commence on X' wins over the allotment/announcement date."""
        text = (
            "The Board of CT Global Managed Portfolio Trust PLC announces that on "
            "8 April 2026 the Company allotted 100,000 Income shares of £0.046131176 each, "
            "from the Company's general business purposes blocklisting facility at a price of "
            "126.00p per Income share. These Income shares will rank pari passu with the "
            "existing Income shares in issue and dealings are expected to commence on "
            "10 April 2026.\n"
            "Total number of Income shares in issue: 61,234,567\n"
            "Total voting rights: 105,347,457\n"
        )
        result = extractor.extract(text, 'CMPI')
        assert result['effective_date'] == '10 April 2026'

    def test_dealings_will_commence(self, extractor):
        """'dealings will commence on X' variant."""
        text = (
            "8 April 2026 The Company allotted 50,000 Growth shares at 198.00p per Growth share. "
            "Dealings will commence on 11 April 2026.\n"
            "Total voting rights: 44,112,890\n"
        )
        result = extractor.extract(text, 'CMPG')
        assert result['effective_date'] == '11 April 2026'

    def test_dealings_commence_without_qualifier(self, extractor):
        """'dealings commence on X' bare variant."""
        text = (
            "8 April 2026 The Company issued 20,000 ordinary shares at 100p. "
            "Dealings commence on 12 April 2026.\n"
            "Total voting rights: 5,000,000\n"
        )
        result = extractor.extract(text, 'XYZ')
        assert result['effective_date'] == '12 April 2026'


class TestCurrencyOverride:
    """Test that non-pence currencies reverse the ×100 conversion."""

    def test_fair_usd_price_not_converted(self, extractor):
        """FAIR is USD — if regex matched £0.42 and converted to 42.0, override reverses it."""
        # Simulate text where regex would match a £ price pattern
        text = ("23 March 2026 Fair Oaks Income 2021 Fund Ltd announces that on 20 March 2026 "
                "it purchased 50,000 ordinary shares at a price of £0.42 per share. "
                "Total voting rights: 100,000,000.")
        result = extractor.extract(text, 'FAIR')
        # Should be 0.42, not 42.0
        assert result['average_price'] == 0.42
        assert result['currency'] == 'USD'

    def test_non_overridden_ticker_keeps_conversion(self, extractor):
        """Normal tickers should keep the £→pence conversion."""
        text = ("23 March 2026 XYZ Fund purchased 10,000 ordinary shares at a price of "
                "£50.28 per share. Total voting rights: 1,000,000.")
        result = extractor.extract(text, 'XYZ')
        assert result['average_price'] == 5028.0


class TestWeeklyAggregation:
    """Test weekly aggregation for tickers like ICG with multi-day tables."""

    def test_icg_weekly_sums_shares_and_vwap(self, extractor):
        text = ("23 March 2026 ICG Enterprise Trust plc announces buyback transactions.\n"
                "Date of purchase       Number of shares    Price paid per share (p)\n"
                "17 March 2026          2,000               1,570.50\n"
                "18 March 2026          3,000               1,574.00\n"
                "19 March 2026          1,500               1,572.00\n"
                "20 March 2026          2,500               1,573.50\n"
                "Total voting rights: 50,000,000.")
        result = extractor.extract(text, 'ICG')
        # Total shares: 2000+3000+1500+2500 = 9000
        assert result['shares_transacted'] == 9000
        # VWAP: (2000*1570.5 + 3000*1574 + 1500*1572 + 2500*1573.5) / 9000
        expected_vwap = round((2000*1570.5 + 3000*1574 + 1500*1572 + 2500*1573.5) / 9000, 2)
        assert result['average_price'] == expected_vwap
        # Last date
        assert result['effective_date'] == '20 March 2026'

    def test_icg_glued_lse_table_uses_vwap_column(self, extractor):
        text = (
            "5 May 2026 ICG plc Total Voting Rights and Transaction in Own Shares. "
            "The Company announces that in the period from 27 April 2026 to 1 May 2026 "
            "it has purchased 660,774 ordinary shares. Aggregated Information\n"
            "Date of Purchase:Aggregate Number of Ordinary Shares Purchased:"
            "Lowest Price Paid per Ordinary Share (GBP):"
            "Highest Price Paid per Ordinary Share (GBP):"
            "Volume-Weighted Average Price Paid per Ordinary Share (GBP):\n"
            "27 April 2026129,0781809.00 pence1831.00 pence1816.15 pence\n"
            "28 April 2026132,3961779.00 pence1810.00 pence1798.49 pence\n"
            "29 April 2026131,316\n\n1779.00 pence1807.00 pence1794.48 pence\n"
            "30 April 2026134,0511768.00 pence1817.00 pence1795.17 pence\n"
            "1 May 2026133,9331825.00 pence1879.00 pence1859.78 pence\n"
            "Total number of voting rights in the Company is 285,212,602."
        )
        result = extractor.extract(text, 'ICG')
        expected_vwap = round(
            (
                129078*1816.15 + 132396*1798.49 + 131316*1794.48
                + 134051*1795.17 + 133933*1859.78
            ) / 660774,
            2,
        )
        assert result['shares_transacted'] == 660774
        assert result['average_price'] == expected_vwap
        assert result['effective_date'] == '01 May 2026'

    def test_cgeo_vertical_weekly_table_sums_shares_and_vwap(self, extractor):
        text = (
            "5 May 2026 Georgia Capital PLC Transaction in Own Shares. "
            "The Company today announces that for the period between 27 April 2026 "
            "and 1 May 2026 it has purchased ordinary shares.\n"
            "Date of purchase:\n"
            "27 April 2026\n28 April 2026\n29 April 2026\n30 April 2026\n1 May 2026\n"
            "Number of shares purchased:\n"
            "5,000\n10,000\n10,000\n10,000\n10,000\n"
            "Volume weighted average price paid per share (pence):\n"
            "4073.1617\n3967.2350\n3883.2381\n3905.5586\n3866.8393\n"
            "Highest price paid per share (pence):\n"
            "4100.0000\n4075.0000\n3935.0000\n3925.0000\n3920.0000\n"
            "Following settlement and cancellation Company will have "
            "34,258,998 ordinary shares in issue."
        )
        result = extractor.extract(text, 'CGEO')
        expected_vwap = round(
            (
                5000*4073.1617 + 10000*3967.2350 + 10000*3883.2381
                + 10000*3905.5586 + 10000*3866.8393
            ) / 45000,
            4,
        )
        assert result['shares_transacted'] == 45000
        assert result['average_price'] == expected_vwap
        assert result['effective_date'] == '01 May 2026'

    def test_non_weekly_ticker_not_aggregated(self, extractor):
        """Normal tickers with multi-date text should NOT be aggregated."""
        text = ("23 March 2026 XYZ Fund purchased 5,000 ordinary shares at 500p per share "
                "on 20 March 2026. Previous purchase: 17 March 2026 3,000 400.00. "
                "Voting rights: 1,000,000.")
        result = extractor.extract(text, 'XYZ')
        # Should not aggregate — XYZ doesn't have weekly_aggregation override
        assert result['shares_transacted'] == 5000


class TestDataQuality:
    def test_ok_when_all_fields_present(self, extractor):
        data = {'average_price': 100.0, 'shares_transacted': 1000, 'transaction_type': 'Buyback', 'shares_outstanding': 1000000}
        assert extractor.assess_quality(data) == 'ok'

    def test_flags_missing_price(self, extractor):
        data = {'average_price': None, 'shares_transacted': 1000, 'transaction_type': 'Buyback', 'shares_outstanding': 1000000}
        assert 'no_price' in extractor.assess_quality(data)
