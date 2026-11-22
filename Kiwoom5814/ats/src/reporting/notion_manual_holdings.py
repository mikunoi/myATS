"""Report account holdings outside the ATS universe without adopting them."""
import json
import time
from decimal import Decimal


class NotionManualHoldingsSync:
    def __init__(self, transport, config, logger):
        self.transport, self.config, self.logger = transport, config, logger
        self.next_attempt = 0
        self.applied = {}

    def sync(self, holdings, excluded, market, name_for, *, force=False):
        if not self.config.get('enabled') or (not force and time.monotonic() < self.next_attempt):
            return []
        self.next_attempt = time.monotonic() + 300
        results = []
        for holding in holdings:
            if holding.quantity <= 0 or holding.symbol in excluded:
                continue
            try:
                symbol = holding.symbol
                currency = 'KRW' if market == 'KRX' else 'USD'
                cost = (Decimal(holding.purchase_amount) if market == 'KRX'
                        else holding.purchase_amount_krw)
                if cost is None:
                    raise ValueError('Missing acquisition cost in KRW')
                average = Decimal(cost) / holding.quantity
                if not average.is_finite() or average <= 0:
                    raise ValueError('Invalid acquisition price')
                fingerprint = (holding.quantity, str(cost))
                if not force and self.applied.get(symbol) == fingerprint:
                    continue
                marker = f'계좌잔고:5814:{market}:{symbol}'
                body = {'page_size': 100, 'filter': {'and': [
                    {'property': '계좌', 'select': {'equals': '키움'}},
                    {'property': '통화', 'select': {'equals': currency}},
                    {'property': '비고', 'rich_text': {'contains': marker}}]}}
                query = self.transport.request('POST', '/v1/data_sources/' + self.config['data_source_id'] + '/query', body)
                rows = query['results']
                if query.get('has_more') or len(rows) > 1:
                    raise ValueError('Ambiguous manual holding pages')
                props = {'상태': {'status': {'name': '전략'}},
                         '리스크': {'select': {'name': '적립식'}},
                         '평단': {'number': float(average)},
                         '보유수량': {'number': holding.quantity}}
                if rows:
                    page = rows[0]['id']
                    self.transport.request('PATCH', '/v1/pages/' + page, {'properties': props})
                else:
                    props.update({'종목': {'title': [{'text': {'content': name_for(symbol)}}]},
                                  '계좌': {'select': {'name': '키움'}},
                                  '통화': {'select': {'name': currency}},
                                  '비고': {'rich_text': [{'text': {'content': marker + ' | 수동 보유 · ATS 미관리'}}]}})
                    created = self.transport.request('POST', '/v1/pages', {
                        'parent': {'type': 'data_source_id', 'data_source_id': self.config['data_source_id']},
                        'properties': props})
                    page = created['id']
                self.applied[symbol] = fingerprint
                results.append({'symbol': symbol, 'page': page, 'quantity': holding.quantity})
            except Exception as error:
                # Reporting must not prevent account reconciliation or evaluations.
                self.logger.error('Manual holding Notion update failed for %s: %s', holding.symbol, type(error).__name__)
                results.append({'symbol': holding.symbol, 'error': type(error).__name__})
        return results
