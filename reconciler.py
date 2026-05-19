"""Day-over-day validation of shares-in-issue arithmetic.

Compares today's extracted data against a prior day's output file to verify:
- Buybacks: prior_shares - shares_transacted = today_shares
- Issuances: prior_shares + shares_issued = today_shares

Flags mismatches for manual review.
"""

import openpyxl


class Reconciler:
    """Validate shares-in-issue math against prior day's data."""

    @staticmethod
    def load_prior_day(filepath):
        """Load prior day's shares-in-issue from Excel file.

        Expects the same column layout as the scraper output:
        Column A = Ticker (with LN suffix), Column J = New shares in issue

        Returns:
            dict: {ticker: shares_in_issue} e.g. {'CGT LN': 15747163}
        """
        prior = {}
        try:
            wb = openpyxl.load_workbook(filepath, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row[0]:
                    continue
                ticker = str(row[0]).strip()
                # Column J = index 9 (New shares in issue)
                shares = row[9] if len(row) > 9 else None
                if shares is not None:
                    try:
                        prior[ticker] = int(shares)
                    except (ValueError, TypeError):
                        pass
            print(f"  Loaded prior day data for {len(prior)} tickers")
        except Exception as e:
            print(f"  Warning: Could not load prior day file: {e}")
        return prior

    @staticmethod
    def validate_row(today_data, prior_shares):
        """Validate one row's shares-in-issue against prior day.

        Args:
            today_data: dict with ticker, transaction_type, shares_transacted, voting_rights
            prior_shares: dict of {ticker: prior_shares_in_issue}

        Returns:
            dict with keys: valid, skipped, expected, actual, difference, reason
        """
        ticker = today_data.get('ticker', '')
        result = {
            'valid': None,
            'skipped': False,
            'expected': None,
            'actual': None,
            'difference': None,
            'reason': None,
        }

        # Skip if ticker not in prior data
        if ticker not in prior_shares:
            result['skipped'] = True
            result['reason'] = 'not_in_prior'
            return result

        # Skip if missing required fields
        shares = today_data.get('shares_transacted')
        today_in_issue = today_data.get('voting_rights')
        tx_type = today_data.get('transaction_type')

        if shares is None or today_in_issue is None or tx_type is None:
            result['skipped'] = True
            result['reason'] = 'missing_fields'
            return result

        prior = prior_shares[ticker]

        # Calculate expected
        if tx_type == 'Buyback':
            expected = prior - shares
        elif tx_type == 'Issuance':
            expected = prior + shares
        else:
            result['skipped'] = True
            result['reason'] = f'unknown_type: {tx_type}'
            return result

        result['expected'] = expected
        result['actual'] = today_in_issue
        result['difference'] = today_in_issue - expected
        result['valid'] = (expected == today_in_issue)

        if not result['valid']:
            result['reason'] = f'expected {expected}, got {today_in_issue} (diff: {result["difference"]})'

        return result

    @staticmethod
    def validate_all(today_results, prior_shares):
        """Validate all rows against prior day data.

        Args:
            today_results: list of dicts (extraction results)
            prior_shares: dict from load_prior_day()

        Returns:
            dict: {ticker: validation_result}
        """
        validations = {}
        for row in today_results:
            ticker = row.get('ticker', '')
            result = Reconciler.validate_row(row, prior_shares)
            if not result['skipped'] and not result['valid']:
                print(f"  Warning: {ticker}: {result['reason']}")
            validations[ticker] = result
        return validations
