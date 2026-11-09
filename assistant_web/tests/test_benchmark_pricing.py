"""캐시 쓰기·미보고 과금·처리 등급을 포함한 비용 비교 회귀."""
import unittest

from scripts.accuracy.benchmark_pricing import price_event


RATES = {'input_per_million': .1, 'cached_input_per_million': .01, 'output_per_million': .5,
         'cache_write_input_per_million': .125, 'cache_write_12h_input_per_million': .25,
         'max_input_tokens': 272000, 'service_tiers': ['default', 'standard'],
         'endpoint_origin': 'https://api.openai.com'}


def event(**usage):
    return {'origin': 'https://api.openai.com', 'service_tier': 'default', 'cache_ttl': '30m',
            'usage': {'prompt': 12000, 'cached': 4000, 'completion': 100, 'cache_write': 7000,
                      'cache_write_30m': 3000, 'cache_write_12h': 4000, **usage}}


class PricingTests(unittest.TestCase):
    def test_cache_writes_replace_ordinary_input_and_are_not_double_charged(self):
        cost, reason = price_event(event(), RATES)
        self.assertEqual(reason, '')
        self.assertAlmostEqual(cost, (1000*.1 + 4000*.01 + 3000*.125 + 4000*.25 + 100*.5)/1e6)

    def test_missing_write_usage_is_unknown_and_explicit_zero_is_priced(self):
        self.assertIsNone(price_event(event(cache_write=None, cache_write_30m=None, cache_write_12h=None), RATES)[0])
        cost, reason = price_event(event(cache_write=0, cache_write_30m=None, cache_write_12h=None), RATES)
        self.assertEqual(reason, '')
        self.assertAlmostEqual(cost, (8000*.1+4000*.01+100*.5)/1e6)

    def test_default_write_lifetime_uses_30m_rate(self):
        cost, reason = price_event(event(cache_write_30m=None, cache_write_12h=None), RATES)
        self.assertEqual(reason, '')
        self.assertAlmostEqual(cost, (1000*.1+4000*.01+7000*.125+100*.5)/1e6)

    def test_invalid_usage_tier_origin_or_long_context_cannot_get_standard_price(self):
        bad = [event(cached=10000), event(cache_write_30m=3001), event(prompt=300000),
               {**event(), 'service_tier': 'priority'}, {**event(), 'service_tier': None},
               {**event(), 'origin': 'https://regional.example'}, {**event(), 'usage': None}]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(price_event(value, RATES)[0])

    def test_legacy_rate_requires_no_cache_write_fields_but_never_ignores_reported_writes(self):
        basic = {k: RATES[k] for k in ('input_per_million', 'cached_input_per_million', 'output_per_million')}
        self.assertIsNotNone(price_event(event(cache_write=None), basic)[0])
        self.assertIsNone(price_event(event(), basic)[0])
