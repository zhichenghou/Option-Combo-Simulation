import pathlib
import sys
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import symbol_mapping


class UnderlyingAndStockMappingTests(unittest.TestCase):
    def test_underlying_etf_maps_to_us_code(self):
        self.assertEqual(
            symbol_mapping.underlying_to_futu_code({'symbol': 'SPY', 'secType': 'STK'}),
            'US.SPY',
        )

    def test_underlying_lowercase_symbol_is_uppercased(self):
        self.assertEqual(
            symbol_mapping.underlying_to_futu_code({'symbol': 'spy'}),
            'US.SPY',
        )

    def test_stock_symbol_maps_to_us_code(self):
        self.assertEqual(symbol_mapping.stock_symbol_to_futu_code('SPY'), 'US.SPY')

    def test_missing_underlying_symbol_raises(self):
        with self.assertRaises(ValueError):
            symbol_mapping.underlying_to_futu_code({})


class OptionMappingTests(unittest.TestCase):
    def test_monthly_call(self):
        code = symbol_mapping.option_to_futu_code({
            'symbol': 'SPY',
            'expDate': '20250620',
            'right': 'C',
            'strike': 500,
        })
        self.assertEqual(code, 'US.SPY250620C500000')

    def test_monthly_put(self):
        code = symbol_mapping.option_to_futu_code({
            'symbol': 'SPY',
            'expDate': '20250620',
            'right': 'P',
            'strike': 500,
        })
        self.assertEqual(code, 'US.SPY250620P500000')

    def test_half_dollar_strike(self):
        code = symbol_mapping.option_to_futu_code({
            'symbol': 'SPY',
            'expDate': '20250620',
            'right': 'P',
            'strike': 450.5,
        })
        self.assertEqual(code, 'US.SPY250620P450500')

    def test_weekly_expiry_date(self):
        code = symbol_mapping.option_to_futu_code({
            'symbol': 'SPY',
            'expDate': '20250613',
            'right': 'C',
            'strike': 500,
        })
        self.assertEqual(code, 'US.SPY250613C500000')

    def test_int_and_float_strike_produce_same_code(self):
        opt_int = {'symbol': 'SPY', 'expDate': '20250620', 'right': 'C', 'strike': 500}
        opt_float = {'symbol': 'SPY', 'expDate': '20250620', 'right': 'C', 'strike': 500.0}
        self.assertEqual(
            symbol_mapping.option_to_futu_code(opt_int),
            symbol_mapping.option_to_futu_code(opt_float),
        )

    def test_lowercase_right_is_normalized(self):
        code = symbol_mapping.option_to_futu_code({
            'symbol': 'spy',
            'expDate': '20250620',
            'right': 'c',
            'strike': 500,
        })
        self.assertEqual(code, 'US.SPY250620C500000')

    def test_missing_strike_raises(self):
        with self.assertRaises(ValueError):
            symbol_mapping.option_to_futu_code({
                'symbol': 'SPY',
                'expDate': '20250620',
                'right': 'C',
            })

    def test_missing_exp_date_raises(self):
        with self.assertRaises(ValueError):
            symbol_mapping.option_to_futu_code({
                'symbol': 'SPY',
                'right': 'C',
                'strike': 500,
            })

    def test_invalid_exp_date_raises(self):
        with self.assertRaises(ValueError):
            symbol_mapping.option_to_futu_code({
                'symbol': 'SPY',
                'expDate': '2025-06-20',
                'right': 'C',
                'strike': 500,
            })

    def test_invalid_right_raises(self):
        with self.assertRaises(ValueError):
            symbol_mapping.option_to_futu_code({
                'symbol': 'SPY',
                'expDate': '20250620',
                'right': 'X',
                'strike': 500,
            })

    def test_non_positive_strike_raises(self):
        with self.assertRaises(ValueError):
            symbol_mapping.option_to_futu_code({
                'symbol': 'SPY',
                'expDate': '20250620',
                'right': 'C',
                'strike': 0,
            })


class OptionRoundTripTests(unittest.TestCase):
    def test_round_trip_restores_fields(self):
        opt = {'symbol': 'SPY', 'expDate': '20250620', 'right': 'C', 'strike': 500}
        parsed = symbol_mapping.parse_futu_option_code(
            symbol_mapping.option_to_futu_code(opt)
        )
        self.assertEqual(parsed['symbol'], 'SPY')
        self.assertEqual(parsed['expDate'], '20250620')
        self.assertEqual(parsed['right'], 'C')
        self.assertEqual(parsed['strike'], 500.0)

    def test_round_trip_half_dollar_strike(self):
        opt = {'symbol': 'SPY', 'expDate': '20250620', 'right': 'P', 'strike': 450.5}
        parsed = symbol_mapping.parse_futu_option_code(
            symbol_mapping.option_to_futu_code(opt)
        )
        self.assertEqual(parsed['strike'], 450.5)

    def test_parse_rejects_non_option_code(self):
        with self.assertRaises(ValueError):
            symbol_mapping.parse_futu_option_code('US.SPY')


if __name__ == '__main__':
    unittest.main()
