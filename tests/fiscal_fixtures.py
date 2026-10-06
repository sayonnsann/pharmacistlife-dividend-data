"""Mark synthetic annual-report fixtures explicitly under the new contract.

These numbers are test inventions. Real private files must be migrated by the
producer, and are deliberately not passed through this fixture helper.
"""
from copy import deepcopy
POLICY_ID = 'issuer-material-display-exclude-haitoukin-2026-10-06'


def annual_report_fixture(records):
    output = deepcopy(records or {})
    for record in output.values():
        if not isinstance(record, dict) or 'series' not in record:
            continue
        external = {str(y) for y in record.get('externalYears', [])}
        record.setdefault('displayPolicy', POLICY_ID)
        record.setdefault('streakSeries', deepcopy(record['series']))
        record.setdefault('displaySeries', {y: v for y, v in record['series'].items() if str(y) not in external})
        record.setdefault('yearProvenance', {str(y): {'sourceCategory': 'a',
            'documentType': 'annualSecuritiesReport', 'displayEligible': True}
            for y in record['displaySeries']})
    return output
