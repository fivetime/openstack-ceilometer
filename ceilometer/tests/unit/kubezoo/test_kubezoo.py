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

import datetime
import os
from unittest import mock

from oslo_messaging._drivers import impl_kafka
from oslo_utils import timeutils
from oslotest import base
from stevedore import extension
import yaml

from ceilometer import declarative
from ceilometer.event import converter
from ceilometer.kubezoo import discovery
from ceilometer.kubezoo import pods
from ceilometer.kubezoo import reconcile
from ceilometer import kubezoo_client
from ceilometer.meter import notifications
from ceilometer.publisher import gnocchi
from ceilometer import sample as sample_util
from ceilometer import service

HERE = os.path.dirname(__file__)
PKG = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))


def _conf(endpoint='http://kubezoo-metering:8090'):
    conf = service.prepare_service([], [])
    conf.set_override('stats_endpoint', endpoint, group='kubezoo')
    return conf


ANSWER = {
    'node': 'incus-node-05',
    'sampled_at': '2026-11-01T11:00:00.000000Z',
    'results': [{
        'pod_uid': '0b6f3e2a-1111-4c2d-9e8f-000000000001',
        'project_id': '9a8b7c6d5e4f40312233445566778899',
        'tenant_id': '111111', 'namespace': 'default',
        'upstream_namespace': '111111-default', 'name': 'web-abcde',
        'owner_kind': 'Deployment', 'owner_name': 'web',
        'node': 'incus-node-05', 'pool': '111111', 'pool_mismatch': False,
        'runtime_class': 'kata', 'qos_class': 'Guaranteed',
        'platform_workload': False, 'phase': 'Running',
        'started_at': '2026-11-01T10:00:17.250000Z',
        'allocation': {'cpu': 1.75, 'memory_mb': 2208.0,
                       'ephemeral_storage_gb': 0.0, 'basis': 'scheduler'},
        'metering': {'metering.cost-center': 'r-and-d'},
    }, {
        # No project: kubezoo-metering lists such a pod as unavailable, but
        # a pollster must not bill one to nobody even if it slipped through.
        'pod_uid': 'no-project', 'project_id': '',
        'allocation': {'cpu': 1.0, 'memory_mb': 1024.0,
                       'ephemeral_storage_gb': 0.0},
    }],
    'unavailable': [],
}


class ClientTest(base.BaseTestCase):

    def test_refuses_without_an_endpoint(self):
        self.assertRaises(ValueError, kubezoo_client.Client, _conf(None))

    def test_token_and_paths(self):
        conf = _conf('http://kubezoo-metering:8090/')
        token = self.create_tempfiles([('token', 's3cret\n')])[0]
        conf.set_override('stats_token_file', token, group='kubezoo')
        client = kubezoo_client.Client(conf)
        self.assertEqual('Bearer s3cret',
                         client.session.headers['Authorization'])
        client.session = mock.Mock()
        client.session.get.return_value.json.return_value = {'nodes': ['a']}
        self.assertEqual(['a'], client.nodes())
        client.node_pods('node/with slash')
        self.assertEqual(
            'http://kubezoo-metering:8090/v1/nodes/node%2Fwith%20slash/pods',
            client.session.get.call_args.args[0])

    def test_an_error_is_raised_not_read_as_empty(self):
        client = kubezoo_client.Client(_conf())
        client.session = mock.Mock()
        client.session.get.return_value.raise_for_status.side_effect = (
            RuntimeError('503 caches not synced yet'))
        self.assertRaises(RuntimeError, client.pods)


class DiscoveryTest(base.BaseTestCase):

    def test_nothing_without_kubezoo(self):
        d = discovery.NodesDiscovery(_conf(None))
        self.assertEqual([], d.discover(mock.Mock()))

    def test_nodes_and_heartbeat(self):
        d = discovery.NodesDiscovery(_conf())
        d._client = mock.Mock()
        d._client.nodes.return_value = ['n1', 'n2']
        manager = mock.Mock()
        found = d.discover(manager)
        self.assertEqual([discovery.NodeToRead('n1'),
                          discovery.NodeToRead('n2')], found)
        self.assertEqual('n1', str(found[0]))
        beats = [c.args[0] for c in manager.heartbeat.call_args_list]
        self.assertEqual(['kubezoo.nodes', 'kubezoo.listed.n1',
                          'kubezoo.listed.n2'], beats)


class PollsterTest(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.conf = _conf()
        self.client = mock.Mock()
        self.client.node_pods.return_value = ANSWER

    def _samples(self, cls, cache=None):
        p = cls(self.conf)
        p._client = self.client
        return list(p.get_samples(mock.Mock(), {} if cache is None else cache,
                                  [discovery.NodeToRead('incus-node-05')]))

    def test_size_is_sampled(self):
        for cls, name, unit, volume in [
                (pods.PodCpuPollster, 'k8s.pod.cpu', 'vcpu', 1.75),
                (pods.PodMemoryPollster, 'k8s.pod.memory', 'MB', 2208.0),
                (pods.PodDiskPollster, 'k8s.pod.disk', 'GB', 0.0)]:
            s = self._samples(cls)
            self.assertEqual(1, len(s), 'the pod with no project is skipped')
            s = s[0]
            self.assertEqual((name, unit, volume), (s.name, s.unit, s.volume))
            self.assertEqual('9a8b7c6d5e4f40312233445566778899', s.project_id)
            self.assertIsNone(s.user_id)
            self.assertEqual('0b6f3e2a-1111-4c2d-9e8f-000000000001',
                             s.resource_id)
            self.assertEqual('2026-11-01T11:00:00.000000Z', s.timestamp)
            md = s.resource_metadata
            self.assertEqual('Running', md['status'])
            self.assertEqual('False', md['platform_workload'])
            self.assertEqual('kata', md['runtime_class'])
            self.assertEqual('scheduler', md['allocation_basis'])
            self.assertEqual({'cost-center': 'r-and-d'}, md['user_metadata'])
            self.assertEqual('2026-11-01T10:00:17.250000Z', md['started_at'])

    def test_one_read_per_node_per_cycle(self):
        cache = {}
        self._samples(pods.PodCpuPollster, cache)
        self._samples(pods.PodMemoryPollster, cache)
        self.client.node_pods.assert_called_once_with('incus-node-05')

    def test_a_node_that_does_not_answer_costs_only_itself(self):
        self.client.node_pods.side_effect = RuntimeError('boom')
        with mock.patch.object(pods.LOG, 'warning') as warn:
            self.assertEqual([], self._samples(pods.PodCpuPollster))
        self.assertIn('incus-node-05', warn.call_args.args[1]['node'])


NOW = datetime.datetime(2026, 11, 1, 12, 0, tzinfo=datetime.UTC)
OLD = '2026-11-01T08:00:00+00:00'


def _resource(rid, started=OLD):
    return {'id': rid, 'original_resource_id': rid, 'started_at': started,
            'metrics': {'k8s.pod.cpu': 'm-' + rid}}


class ReconcileTest(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.kubezoo = mock.Mock()
        self.kubezoo.pods.return_value = [
            {'pod_uid': 'running', 'billing': True},
            {'pod_uid': 'finished', 'billing': False,
             'ended_at': '2026-11-01T09:30:00.000000Z'},
            {'pod_uid': 'clock-skew', 'billing': False,
             'ended_at': '2026-11-01T07:00:00.000000Z'},
        ]
        self.gnocchi = mock.Mock()
        self.open = []
        self.gnocchi.resource.search.side_effect = (
            lambda resource_type, query, limit, marker: self.open)
        self.gnocchi.metric.get_measures.return_value = [
            ['2026-11-01T09:00:00+00:00', 60.0, 1.0],
            ['2026-11-01T09:41:00+00:00', 60.0, 1.0]]

    def _run(self, **kw):
        return reconcile.reconcile(self.kubezoo, self.gnocchi, now=NOW, **kw)

    def test_billing_is_left(self):
        self.open = [_resource('running')]
        self.assertEqual([], self._run())
        self.gnocchi.resource.update.assert_not_called()

    def test_finished_ends_at_its_own_end(self):
        self.open = [_resource('finished')]
        self._run()
        self.gnocchi.resource.update.assert_called_once_with(
            'k8s_pod', 'finished', {'ended_at': '2026-11-01T09:30:00+00:00'})

    def test_never_before_the_start(self):
        self.open = [_resource('clock-skew')]
        self._run()
        self.gnocchi.resource.update.assert_called_once_with(
            'k8s_pod', 'clock-skew', {'ended_at': '2026-11-01T08:00:00+00:00'})

    def test_gone_ends_when_last_seen_and_is_marked(self):
        self.open = [_resource('gone')]
        self._run()
        self.gnocchi.resource.update.assert_called_once_with(
            'k8s_pod', 'gone', {'ended_at': '2026-11-01T09:41:00+00:00',
                                'ended_by': 'reconcile'})

    def test_young_is_left(self):
        self.open = [_resource('gone', started='2026-11-01T11:55:00+00:00')]
        self.assertEqual([], self._run())

    def test_cannot_ask_ends_nothing(self):
        self.open = [_resource('gone')]
        self.kubezoo.pods.side_effect = RuntimeError('503')
        self.assertRaises(RuntimeError, self._run)
        self.gnocchi.resource.update.assert_not_called()

    def test_dry_run(self):
        self.open = [_resource('gone')]
        self.assertEqual(
            [('k8s_pod', 'gone', '2026-11-01T09:41:00+00:00')],
            self._run(dry_run=True))
        self.gnocchi.resource.update.assert_not_called()

    def test_one_failure_does_not_stop_the_rest(self):
        self.open = [_resource('gone'), _resource('finished')]
        self.gnocchi.resource.update.side_effect = [Exception('400'), None]
        e = self.assertRaises(reconcile.Incomplete, self._run)
        self.assertEqual(['finished'], [rid for _, rid, _ in e.ended])
        self.assertIn('k8s_pod', str(e))


class DefinitionsTest(base.BaseTestCase):
    """The package's own definitions read kubezoo-metering's own bytes.

    data/ is generated by kubezoo-metering (hack/ceilometer/fromgo), so a
    change on either side that breaks the other fails here.
    """

    def setUp(self):
        super().setUp()
        self.conf = service.prepare_service([], [])
        self.traits = extension.ExtensionManager('ceilometer.event.trait_plugin')

    def _notification(self, name):
        with open(os.path.join(HERE, 'data', name + '.json'), 'rb') as f:
            ctxt, msg = impl_kafka.unpack_message(f.read())
        # As oslo.messaging's NotificationDispatcher builds it.
        return dict(ctxt=ctxt, publisher_id=msg['publisher_id'],
                    event_type=msg['event_type'], payload=msg['payload'],
                    metadata={'message_id': msg['message_id'],
                              'timestamp': msg['timestamp']})

    def _samples(self, n):
        cfg = declarative.load_definitions(
            self.conf, {}, os.path.join(PKG, 'data', 'meters.d',
                                        'meters.yaml'))
        out = []
        for m in cfg['metric']:
            md = notifications.MeterDefinition(m, self.conf, self.traits)
            if md.match_type(n['event_type']):
                out.extend(sample_util.Sample.from_notification(**s)
                           for s in md.to_samples(n))
        return out

    def test_one_pods_life(self):
        create = self._notification('k8s.pod.create.end')
        delete = self._notification('k8s.pod.delete.end')
        started = timeutils.parse_isotime(create['payload']['started_at'])
        ended = timeutils.parse_isotime(delete['payload']['ended_at'])

        conv = converter.setup_events(self.conf, self.traits)
        ce = conv.to_event('info', create)
        de = conv.to_event('info', delete)
        self.assertEqual(timeutils.normalize_time(started), ce.generated)
        self.assertEqual(timeutils.normalize_time(ended), de.generated)
        traits = {t.name: t.value for t in ce.traits}
        self.assertEqual(1.75, traits['cpu'])
        self.assertEqual('True' if create['payload']['platform_workload']
                         else 'False', traits['platform_workload'])

        with open(os.path.join(PKG, 'publisher', 'data',
                               'gnocchi_resources.yaml')) as f:
            rd_cfg = [r for r in yaml.safe_load(f)['resources']
                      if r['resource_type'] == 'k8s_pod'][0]
        rd = gnocchi.ResourcesDefinition(rd_cfg, 'low', {}, self.traits)
        attrs = rd.event_attributes(ce)
        self.assertEqual(create['payload']['pod_uid'], attrs['id'])
        self.assertEqual(timeutils.normalize_time(started),
                         attrs['started_at'])
        self.assertEqual('k8s.pod.delete.end', rd_cfg['event_delete'])

        made = self._samples(create)
        self.assertEqual(['k8s.pod.cpu', 'k8s.pod.disk', 'k8s.pod.memory'],
                         sorted(s.name for s in made))
        # Whichever reaches Gnocchi first creates the resource, so a sample
        # must carry the real start as well as the event does.
        for s in made:
            self.assertEqual(create['payload']['started_at'],
                             rd.sample_attributes(s)['started_at'])
        ends = self._samples(delete)
        self.assertEqual(['k8s.pod.lifetime'], [s.name for s in ends])
        self.assertEqual((ended - started).total_seconds(), ends[0].volume)

        # Gnocchi keeps a resource ended only while ended_at is after its
        # latest measure.
        latest = max(timeutils.normalize_time(
            timeutils.parse_isotime(s.timestamp)) for s in made + ends)
        self.assertLess(latest, de.generated)
