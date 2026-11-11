"""Contract 1.5: events carry series_id, and calendar_update_event scope series moves every repeat (wizard-calendar#33)."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import graph  # noqa: E402

MASTER = f'{graph.GRAPH}/me/events/SM1'


class FakeCalendar:
    """Just enough of Graph: a weekly series master in W. Europe time (10:00-10:30 local = 09:00 UTC), one occurrence of
    it, and a single event. GET honours Prefer outlook.timezone for the master (UTC or the series' own zone)."""

    def __init__(self, zone='W. Europe Standard Time'):
        self.master = {'id': 'SM1', 'type': 'seriesMaster', 'subject': 'Standup', 'isAllDay': False,
                       'originalStartTimeZone': zone, 'originalEndTimeZone': zone,
                       'local': {'start': '2026-11-02T10:00:00.0000000', 'end': '2026-11-02T10:30:00.0000000'}}
        self.items = {'OC1': {'id': 'OC1', 'type': 'occurrence', 'seriesMasterId': 'SM1'},
                      'ONE': {'id': 'ONE', 'type': 'singleInstance'}}
        self.calls = []

    def __call__(self, method, url, query=None, body=None, headers=None):
        self.calls.append((method, url, copy.deepcopy(query), copy.deepcopy(body), dict(headers or {})))
        eid = url.rsplit('/', 1)[-1]
        prefer = (headers or {}).get('Prefer', '')
        if eid == 'SM1':
            if method == 'PATCH':
                assert body['start']['timeZone'] == self.master['originalStartTimeZone'], body
                self.master['local'] = {'start': body['start']['dateTime'] + '.0000000', 'end': body['end']['dateTime'] + '.0000000'}
                return {k: v for k, v in self.master.items() if k != 'local'}
            out = {k: v for k, v in self.master.items() if k != 'local'}
            if f'outlook.timezone="{self.master["originalStartTimeZone"]}"' in prefer:
                tz = self.master['originalStartTimeZone']
                out.update(start={'dateTime': self.master['local']['start'], 'timeZone': tz},
                           end={'dateTime': self.master['local']['end'], 'timeZone': tz})
            return out
        if method == 'GET' and eid in self.items:
            return copy.deepcopy(self.items[eid])
        raise AssertionError((method, url))


class SeriesId(unittest.TestCase):
    def test_events_carry_their_series(self):
        self.assertEqual(graph._event({'id': 'OC1', 'type': 'occurrence', 'seriesMasterId': 'SM1'}, 'primary')['series_id'], 'SM1')
        self.assertEqual(graph._event({'id': 'SM1', 'type': 'seriesMaster'}, 'primary')['series_id'], 'SM1')
        self.assertIsNone(graph._event({'id': 'ONE', 'type': 'singleInstance'}, 'primary')['series_id'])


class SeriesShift(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCalendar()

    def update(self, by='owner', **args):
        return graph.calendar_update_event(self.fake, args, by=by)

    def patches(self):
        return [c for c in self.fake.calls if c[0] == 'PATCH']

    def test_moving_one_repeat_moves_the_series_in_its_own_zone(self):
        out = self.update(id='OC1', scope='series', shift_start_minutes=90, shift_end_minutes=90)
        self.assertEqual((out['scope'], out['series_id'], out['updated']), ('series', 'SM1', True))
        [(_, url, _, body, _)] = self.patches()
        self.assertEqual(url, MASTER)
        self.assertEqual(body['start'], {'dateTime': '2026-11-02T11:30:00', 'timeZone': 'W. Europe Standard Time'})
        self.assertEqual(body['end'], {'dateTime': '2026-11-02T12:00:00', 'timeZone': 'W. Europe Standard Time'})
        self.assertEqual(body['singleValueExtendedProperties'], [{'id': graph.BY_PROP, 'value': 'owner'}])
        self.assertNotIn('recurrence', body)  # the pattern is left alone
        # Undo: the opposite shift on the series id.
        self.update(id='SM1', scope='series', shift_start_minutes=-90, shift_end_minutes=-90)
        self.assertEqual(self.fake.master['local'], {'start': '2026-11-02T10:00:00.0000000', 'end': '2026-11-02T10:30:00.0000000'})

    def test_resize_moves_only_the_end_and_agents_are_marked(self):
        self.update(by='agent', id='SM1', scope='series', shift_end_minutes=30)
        body = self.patches()[-1][3]
        self.assertEqual((body['start']['dateTime'], body['end']['dateTime']), ('2026-11-02T10:00:00', '2026-11-02T11:00:00'))
        self.assertEqual(body['singleValueExtendedProperties'], [{'id': graph.BY_PROP, 'value': 'agent'}])

    def test_utc_and_custom_zones(self):
        utc = FakeCalendar('tzone://Microsoft/Utc')
        utc.master['originalStartTimeZone'] = utc.master['originalEndTimeZone'] = 'UTC'
        graph.calendar_update_event(utc, {'id': 'SM1', 'scope': 'series', 'shift_start_minutes': 15, 'shift_end_minutes': 15}, by='owner')
        self.assertEqual(utc.master['local']['start'], '2026-11-02T10:15:00.0000000')
        custom = FakeCalendar('tzone://Microsoft/Custom')
        with self.assertRaises(graph.BadArgs):
            graph.calendar_update_event(custom, {'id': 'SM1', 'scope': 'series', 'shift_start_minutes': 15, 'shift_end_minutes': 15})
        self.assertFalse([c for c in custom.calls if c[0] == 'PATCH'])

    def test_refusals_change_nothing(self):
        before = copy.deepcopy(self.fake.master)
        cases = [
            dict(id='ONE', scope='series', shift_start_minutes=30, shift_end_minutes=30),       # not repeating
            dict(id='OC1', scope='series', shift_start_minutes=-660, shift_end_minutes=-660),   # to Sunday
            dict(id='OC1', scope='series', shift_start_minutes=840, shift_end_minutes=840),     # past midnight
            dict(id='OC1', scope='series', shift_start_minutes=60),                             # ends before start
            dict(id='OC1', scope='series'),
            dict(id='OC1', scope='series', shift_start_minutes=5000),
            dict(id='OC1', scope='series', shift_start_minutes=True),
            dict(id='OC1', scope='series', end='2026-11-02T11:00:00Z', shift_start_minutes=5),
            dict(id='OC1', scope='all', title='x'),
            dict(id='OC1', title='x', shift_end_minutes=5),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(graph.BadArgs):
                self.update(**args)
        self.assertEqual(self.fake.master, before)
        self.assertFalse(self.patches())

    def test_schema_lists_the_new_arguments(self):
        tool = next(t for t in graph.mcp_tools() if t['name'] == 'calendar_update_event')
        self.assertEqual(tool['inputSchema']['properties']['scope']['enum'], ['occurrence', 'series'])


if __name__ == '__main__':
    unittest.main()
