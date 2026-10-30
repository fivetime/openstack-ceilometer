#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""A Zun object whose delete event was lost still ends, when last seen."""

import datetime
from unittest import mock

from oslotest import base

from ceilometer.container import reconcile

NOW = datetime.datetime(2026, 10, 17, 12, 0, tzinfo=datetime.UTC)
OLD = '2026-10-17T08:00:00+00:00'


def _resource(rid, host='node-1', started=OLD, metrics=None):
    return {'id': rid, 'host': host, 'started_at': started,
            'metrics': metrics if metrics is not None else {'cpu': 'm-' + rid}}


class ReconcileTest(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.zun = mock.Mock()
        self.zun.hosts.return_value = ['node-1', 'node-2']
        self.answers = {
            'node-1': {'results': [{'uuid': 'alive'}],
                       'unavailable': [{'uuid': 'unread'}]},
            'node-2': {'results': [], 'unavailable': []},
        }
        self.zun.host_stats.side_effect = (
            lambda host, kinds: self.answers[host])
        self.gnocchi = mock.Mock()
        self.open = {'zun_container': [], 'zun_capsule': []}
        self.gnocchi.resource.search.side_effect = (
            lambda resource_type, query, limit, marker:
            self.open[resource_type])
        self.gnocchi.metric.get_measures.return_value = [
            ['2026-10-17T09:00:00+00:00', 300.0, 1.0],
            ['2026-10-17T09:55:00+00:00', 300.0, 1.0]]

    def _run(self, **kw):
        return reconcile.reconcile(self.zun, self.gnocchi, now=NOW, **kw)

    def test_gone_is_ended_when_last_seen_and_marked(self):
        self.open['zun_container'] = [_resource('gone')]

        ended = self._run()

        self.assertEqual([('zun_container', 'gone',
                           '2026-10-17T09:55:00+00:00')], ended)
        self.gnocchi.resource.update.assert_called_once_with(
            'zun_container', 'gone',
            {'ended_at': '2026-10-17T09:55:00+00:00',
             'ended_by': 'reconcile'})

    def test_what_zun_still_has_is_left(self):
        self.open['zun_container'] = [_resource('alive'),
                                      _resource('unread')]

        self.assertEqual([], self._run())
        self.gnocchi.resource.update.assert_not_called()

    def test_a_host_that_did_not_answer_is_left_alone(self):
        """Not being able to ask is not an answer."""
        self.answers['node-2'] = None

        def answer(host, kinds):
            if host == 'node-2':
                raise RuntimeError('timed out')
            return self.answers[host]
        self.zun.host_stats.side_effect = answer
        self.open['zun_container'] = [_resource('x', host='node-2')]

        self.assertEqual([], self._run())

    def test_a_young_resource_is_left(self):
        """It may have been created after its host was read."""
        self.open['zun_capsule'] = [
            _resource('new', started='2026-10-17T11:55:00+00:00')]

        self.assertEqual([], self._run())

    def test_a_host_zun_no_longer_has_ends_what_was_on_it(self):
        self.open['zun_capsule'] = [_resource('c', host='retired')]

        ended = self._run()

        self.assertEqual(['c'], [rid for _, rid, _ in ended])

    def test_measures_waiting_to_be_aggregated_count(self):
        """A dry run and the run after it name the same time."""
        self.open['zun_container'] = [_resource('gone')]

        self._run(dry_run=True)

        self.assertTrue(all(
            c.kwargs.get('refresh')
            for c in self.gnocchi.metric.get_measures.call_args_list))

    def test_never_measured_ends_when_it_started(self):
        self.open['zun_container'] = [_resource('quiet', metrics={})]

        ended = self._run()

        self.assertEqual(OLD, ended[0][2])

    def test_a_dry_run_changes_nothing(self):
        self.open['zun_container'] = [_resource('gone')]

        ended = self._run(dry_run=True)

        self.assertEqual(1, len(ended))
        self.gnocchi.resource.update.assert_not_called()

    def test_only_open_resources_are_asked_for(self):
        self._run()

        for call in self.gnocchi.resource.search.call_args_list:
            self.assertEqual({'=': {'ended_at': None}},
                             call.kwargs['query'])

    def test_a_last_measure_before_the_start_ends_it_when_it_started(self):
        """A measure is stamped with the start of its bucket.

        Created at 00:30:47 and gone before 00:31, its last measure reads
        00:30:00 -- before it started, which Gnocchi refuses (400).
        """
        started = '2026-10-17T09:55:47+00:00'
        self.open['zun_container'] = [_resource('brief', started=started)]

        ended = self._run()

        self.assertEqual([('zun_container', 'brief', started)], ended)

    def test_one_gnocchi_refuses_does_not_stop_the_rest(self):
        self.open['zun_container'] = [_resource('refused'), _resource('ok')]
        self.open['zun_capsule'] = [_resource('cap')]

        def update(resource_type, rid, attrs):
            if rid == 'refused':
                raise RuntimeError('HTTP 400')
        self.gnocchi.resource.update.side_effect = update

        exc = self.assertRaises(reconcile.Incomplete, self._run)

        self.assertEqual(['ok', 'cap'], [rid for _, rid, _ in exc.ended])
        self.assertEqual(['refused'], [rid for _, rid, _ in exc.failed])
        self.assertEqual(3, self.gnocchi.resource.update.call_count)
