"""출처 있는 단가의 적용 범위와 실제 과금 토큰을 대조한다."""
from datetime import date
import math


def validate_prices(prices: dict) -> dict:
    if not prices:
        return {}
    if not all(isinstance(prices.get(k), str) and prices[k] for k in ('currency', 'as_of', 'source')):
        raise ValueError('단가에는 통화·기준일·출처가 필요합니다')
    date.fromisoformat(prices['as_of'])
    if not isinstance(prices.get('models'), dict) or not prices['models']:
        raise ValueError('모델별 단가가 필요합니다')
    for rates in prices['models'].values():
        for name in ('input_per_million', 'cached_input_per_million', 'output_per_million',
                     *[k for k in ('cache_write_input_per_million', 'cache_write_12h_input_per_million') if k in rates]):
            value = rates.get(name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('토큰 단가는 유한한 0 이상의 숫자여야 합니다')
        if 'max_input_tokens' in rates and (type(rates['max_input_tokens']) is not int or rates['max_input_tokens'] <= 0):
            raise ValueError('입력 토큰 가격 구간이 잘못됐습니다')
        if 'service_tiers' in rates and (not isinstance(rates['service_tiers'], list) or not rates['service_tiers']
                or any(not isinstance(v, str) or not v for v in rates['service_tiers'])):
            raise ValueError('처리 등급 목록이 잘못됐습니다')
    return prices


def price_event(event: dict, rates: dict) -> tuple[float | None, str]:
    used = event.get('usage')
    if not used:
        return None, 'usage_missing'
    if rates.get('endpoint_origin') and event.get('origin') != rates['endpoint_origin']:
        return None, 'origin_unpriced'
    if rates.get('service_tiers') and event.get('service_tier') not in rates['service_tiers']:
        return None, 'tier_unpriced'
    if rates.get('max_input_tokens') and used['prompt'] > rates['max_input_tokens']:
        return None, 'context_unpriced'
    write = used.get('cache_write')
    if 'cache_write_input_per_million' in rates and write is None:
        return None, 'cache_write_usage_missing'
    write = 0 if write is None else write
    if any(type(v) is not int or v < 0 for v in (used['prompt'], used['completion'], used['cached'], write)):
        return None, 'invalid_usage'
    ordinary = used['prompt'] - used['cached'] - write
    if ordinary < 0:
        return None, 'invalid_usage'
    write_cost = 0.0
    if write:
        if 'cache_write_input_per_million' not in rates:
            return None, 'cache_write_unpriced'
        short, long = used.get('cache_write_30m'), used.get('cache_write_12h')
        if short is None and long is None:
            if event.get('cache_ttl') not in {'30m', '12h'}:
                return None, 'cache_lifetime_unknown'
            short, long = (write, 0) if event['cache_ttl'] == '30m' else (0, write)
        if any(type(v) is not int or v < 0 for v in (short, long)) or short + long != write:
            return None, 'invalid_cache_write_breakdown'
        if long and 'cache_write_12h_input_per_million' not in rates:
            return None, 'cache_lifetime_unpriced'
        write_cost = short * rates['cache_write_input_per_million'] + long * rates.get('cache_write_12h_input_per_million', 0)
    return ((ordinary * rates['input_per_million'] + used['cached'] * rates['cached_input_per_million']
             + write_cost + used['completion'] * rates['output_per_million']) / 1e6, '')
