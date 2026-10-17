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
"""Zun's containers and capsules, polled per host and told by events."""

from unittest import mock

from oslotest import base
import yaml

from ceilometer.container import discovery
from ceilometer.container import zun
from ceilometer.event import converter
from ceilometer.meter import notifications
from ceilometer import service
from ceilometer import zun_client


def _conf():
    return service.prepare_service([], [])


class ZunClientTest(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.conf = _conf()
        with mock.patch('ceilometer.keystone_client.get_session'):
            self.client = zun_client.Client(self.conf)
        self.client.api = mock.Mock()
        self.client.api.get_endpoint.return_value = 'http://zun:9517/v1'

    def _page(self, hosts, more):
        page = mock.Mock()
        page.json.return_value = {
            'hosts': [{'hostname': h, 'uuid': 'u-' + h} for h in hosts],
            'next': 'http://zun/v1/hosts?marker=x' if more else None}
        return page

    def test_every_page_of_hosts_is_followed(self):
        """Thousands of hosts do not fit in one page."""
        self.client.api.get.side_effect = [
            self._page(['a', 'b'], more=True), self._page(['c'], more=False)]

        self.assertEqual(['a', 'b', 'c'], self.client.hosts())
        second = self.client.api.get.call_args_list[1]
        self.assertEqual({'marker': 'u-b'}, second.kwargs['params'])

    def test_one_host_is_asked_for_the_kinds_named(self):
        self.client.api.get.return_value.json.return_value = {'results': []}

        self.client.host_stats('node-1', ['capsule'],
                               label='metering.autoscale')

        self.client.api.get.assert_called_once_with(
            'hosts/node-1/stats',
            params={'type': 'capsule', 'label': 'metering.autoscale'})

    def test_an_endpoint_with_the_version_is_not_given_it_twice(self):
        """Zun registers .../v1; asking for /v1/hosts there was a 404."""
        self.client.api.get.side_effect = [self._page(['a'], more=False)]

        self.client.hosts()

        self.assertEqual('hosts', self.client.api.get.call_args.args[0])

    def test_an_endpoint_without_the_version_is_given_it(self):
        self.client.api.get_endpoint.return_value = 'http://zun:9517/'
        self.client.api.get.side_effect = [self._page(['a'], more=False)]

        self.client.hosts()

        self.assertEqual('v1/hosts', self.client.api.get.call_args.args[0])

    def test_it_asks_for_the_version_with_host_stats(self):
        with mock.patch('ceilometer.keystone_client.get_session'):
            client = zun_client.Client(self.conf)
        self.assertEqual('1.56', client.api.default_microversion)
        self.assertEqual('container', client.api.service_type)


class DiscoveryTest(base.BaseTestCase):

    def _discover(self, cls, param=None, conf=None):
        conf = conf or _conf()
        with mock.patch.object(zun_client, 'Client') as client:
            client.return_value.hosts.return_value = ['a', 'b']
            return cls(conf).discover(mock.Mock(), param)

    def test_a_host_is_the_unit_handed_out(self):
        found = self._discover(discovery.ContainersDiscovery)

        self.assertEqual([discovery.HostToRead('a', 'container'),
                          discovery.HostToRead('b', 'container')], found)

    def test_partitioning_is_by_host_alone(self):
        """Every kind read from one host lands on the same agent."""
        self.assertEqual('a', str(discovery.HostToRead('a', 'capsule',
                                                       'metering.x')))

    def test_a_label_narrows_what_is_read(self):
        found = self._discover(discovery.CapsulesDiscovery,
                               param='label=metering.autoscale')

        self.assertEqual('metering.autoscale', found[0].label)
        self.assertEqual('capsule', found[0].kind)

    def test_capsule_containers_are_off_by_default(self):
        """Their allocation is counted in their capsule's."""
        self.assertEqual(
            [], self._discover(discovery.CapsuleContainersDiscovery))

    def test_capsule_containers_when_enabled(self):
        conf = _conf()
        conf.set_override('meter_capsule_containers', True, group='zun')

        found = self._discover(discovery.CapsuleContainersDiscovery,
                               conf=conf)

        self.assertEqual(['capsule_container'] * 2, [f.kind for f in found])

    def test_it_needs_zun_in_the_catalog(self):
        self.assertEqual('zun',
                         discovery.ContainersDiscovery
                         .KEYSTONE_REQUIRED_FOR_SERVICE)


def _entry(uuid, kind, **kw):
    entry = {
        'uuid': uuid, 'container_type': kind, 'name': uuid,
        'project_id': 'p-1', 'user_id': 'u-1', 'host': 'node-1',
        'image': 'img', 'runtime': 'kata-qemu', 'status': 'Running',
        'started_at': '2026-10-17T00:00:00+00:00',
        'allocation': {'cpu': 1.0, 'memory_mb': 512, 'disk_gb': 10},
        'labels': {'metering.server_group': 'web', 'private': 'x'},
        'reading': {
            'timestamp': '2026-10-17T01:02:03.000004+00:00',
            'cpu': {'total_ns': 5000},
            'memory': {'usage': 3 * 1024 * 1024, 'cache': 1024 * 1024},
            'networks': {'eth0': {'rx_bytes': 10, 'tx_bytes': 20},
                         'eth1': {'rx_bytes': 1, 'tx_bytes': 2}},
            'blkio': {'read_bytes': 7, 'write_bytes': 8}},
        'previous': None,
    }
    entry.update(kw)
    return entry


class PollsterTest(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.conf = _conf()
        self.client = mock.Mock()
        self.client.host_stats.return_value = {'results': [
            _entry('c-1', 'container'),
            _entry('cap-1', 'capsule', children=['cc-1'],
                   allocation_basis='max(...)'),
            _entry('cc-1', 'capsule_container', capsule_uuid='cap-1',
                   allocation_included_in_parent=True),
        ], 'unavailable': []}
        self.host = discovery.HostToRead('node-1', 'container')

    def _samples(self, cls, cache=None, resources=None):
        pollster = cls(self.conf)
        pollster._client = self.client
        return list(pollster.get_samples(
            mock.Mock(), {} if cache is None else cache,
            resources or [self.host]))

    def test_what_was_allocated(self):
        for cls, volume, unit in ((zun.ContainerCpuPollster, 1.0, 'vcpu'),
                                  (zun.ContainerMemoryPollster, 512, 'MB'),
                                  (zun.ContainerDiskPollster, 10, 'GB')):
            (s,) = self._samples(cls)
            self.assertEqual((volume, unit), (s.volume, s.unit))

    def test_what_was_used(self):
        def one(cls):
            (s,) = self._samples(cls)
            return s.volume
        self.assertEqual(5000, one(zun.ContainerCpuTimePollster))
        # the working set: usage less the inactive page cache, in MB
        self.assertEqual(2.0, one(zun.ContainerMemoryUsagePollster))
        self.assertEqual(11, one(zun.ContainerNetworkIncomingBytesPollster))
        self.assertEqual(22, one(zun.ContainerNetworkOutgoingBytesPollster))
        self.assertEqual(7, one(zun.ContainerDiskReadBytesPollster))
        self.assertEqual(8, one(zun.ContainerDiskWriteBytesPollster))

    def test_stamped_with_when_the_node_read_it(self):
        (s,) = self._samples(zun.ContainerCpuTimePollster)
        self.assertEqual('2026-10-17T01:02:03.000004+00:00', s.timestamp)

    def test_each_kind_reads_only_its_own(self):
        names = {s.name: s.resource_id for cls in (
            zun.ContainerCpuPollster, zun.CapsuleCpuPollster,
            zun.CapsuleContainerCpuPollster) for s in self._samples(cls)}
        self.assertEqual({'container.cpu': 'c-1', 'capsule.cpu': 'cap-1',
                          'capsule.container.cpu': 'cc-1'}, names)

    def test_a_capsule_container_points_at_its_capsule(self):
        (s,) = self._samples(zun.CapsuleContainerCpuPollster)
        self.assertEqual('cap-1', s.resource_metadata['capsule_uuid'])
        self.assertTrue(
            s.resource_metadata['allocation_included_in_parent'])

    def test_metering_labels_as_user_metadata(self):
        """Carried the way nova carries an instance's metering metadata."""
        (s,) = self._samples(zun.ContainerCpuPollster)
        self.assertEqual({'server_group': 'web'},
                         s.resource_metadata['user_metadata'])

    def test_one_call_per_host_per_cycle_whatever_the_meters(self):
        cache = {}
        for cls in (zun.ContainerCpuPollster, zun.ContainerMemoryPollster,
                    zun.ContainerCpuTimePollster):
            self._samples(cls, cache=cache)
        self.assertEqual(1, self.client.host_stats.call_count)

    def test_a_figure_the_node_did_not_read_is_not_sampled(self):
        entry = _entry('c-1', 'container')
        entry['reading'].pop('networks')
        entry['reading'].pop('blkio')
        self.client.host_stats.return_value = {'results': [entry]}
        for cls in (zun.ContainerNetworkIncomingBytesPollster,
                    zun.ContainerDiskReadBytesPollster):
            self.assertEqual([], self._samples(cls))

    def test_a_host_that_cannot_be_read_costs_only_itself(self):
        def answer(host, kinds, label=None):
            if host == 'bad':
                raise RuntimeError('timed out')
            return {'results': [_entry('c-1', 'container')]}
        self.client.host_stats.side_effect = answer
        hosts = [discovery.HostToRead('bad', 'container'), self.host]

        with mock.patch.object(zun.LOG, 'warning') as warning:
            samples = self._samples(zun.ContainerCpuPollster,
                                    resources=hosts)

        self.assertEqual(['c-1'], [s.resource_id for s in samples])
        self.assertIn('bad', str(warning.call_args))

    def test_twenty_seven_pollsters_are_registered(self):
        polls = {k: v for k, v in _central_pollsters().items()
                 if v.startswith('ceilometer.container.zun:')}
        self.assertEqual(27, len(polls))
        for name, target in polls.items():
            cls = getattr(zun, target.split(':')[1])
            self.assertEqual(name, '%s.%s' % (
                zun.KINDS[cls.KIND]['prefix'], cls.METER))


def _central_pollsters():
    """ceilometer.poll.central as registered, wherever this branch keeps it.

    pyproject.toml on master; setup.cfg on branches that predate the move.
    """
    import configparser
    import tomllib
    with open('pyproject.toml', 'rb') as f:
        eps = tomllib.load(f).get('project', {}).get('entry-points', {})
    if 'ceilometer.poll.central' in eps:
        return dict(eps['ceilometer.poll.central'])
    cfg = configparser.ConfigParser()
    cfg.read('setup.cfg')
    lines = cfg['entry_points']['ceilometer.poll.central'].splitlines()
    return dict(tuple(p.strip() for p in line.split('=', 1))
                for line in lines if '=' in line)


def _notification(event_type, **payload):
    body = {
        'payload_version': '2.0', 'container_type': 'container',
        'occurred_at': '2026-10-17T03:00:00+00:00',
        'uuid': 'c-1', 'name': 'web', 'user_id': 'u-1',
        'tenant_id': 'p-1', 'project_id': 'p-1', 'host': 'node-1',
        'image': 'img', 'status': 'Running', 'task_state': None,
        'cpu': 1.0, 'memory': '512', 'disk': 10, 'runtime': 'kata-qemu',
        'created_at': '2026-10-17T00:00:00+00:00',
        'started_at': '2026-10-17T00:00:10+00:00',
        'labels': {'metering.server_group': 'web'},
    }
    body.update(payload)
    return {'event_type': event_type, 'priority': 'INFO',
            'publisher_id': 'zun.node-1',
            'metadata': {'timestamp': '2026-10-17T03:00:00.000001',
                         'message_id': 'm-1'},
            'ctxt': {}, 'payload': body}


class ShippedMeterDefinitionsTest(base.BaseTestCase):
    """Zun's notifications through the meter definitions ceilometer ships.

    Real definitions, real payloads -- the fields zun's notifications carry
    -- so a definition that cannot read what zun sends fails here.
    """

    def setUp(self):
        super().setUp()
        self.handler = notifications.ProcessMeterNotifications(
            _conf(), mock.Mock())

    def _samples(self, notification):
        return {s.name: s for s in self.handler.build_sample(notification)}

    def test_allocation_on_every_end_stamped_when_it_happened(self):
        found = self._samples(_notification('container.start.end'))

        self.assertEqual({'container.cpu', 'container.memory',
                          'container.disk'}, set(found))
        self.assertEqual(512, float(found['container.memory'].volume))
        self.assertEqual('2026-10-17T03:00:00+00:00',
                         found['container.cpu'].timestamp)
        self.assertEqual('web', found['container.cpu'].resource_metadata[
            'user_metadata']['server_group'])

    def test_how_long_it_lived_and_what_it_used_on_delete(self):
        found = self._samples(_notification(
            'container.delete.end',
            last_reading={'timestamp': '2026-10-17T02:59:59+00:00',
                          'cpu': {'total_ns': 123}}))

        self.assertEqual(10790.0, found['container.lifetime'].volume)
        self.assertEqual(123, found['container.cpu.time'].volume)
        self.assertEqual('2026-10-17T02:59:59+00:00',
                         found['container.cpu.time'].timestamp)

    def test_no_last_reading_is_no_figure(self):
        """Left without a volume, which the sample pipeline drops.

        A container that had stopped on its own has nothing left to read;
        a zero would claim it used nothing.
        """
        found = self._samples(_notification('container.delete.end'))

        self.assertIsNone(found['container.cpu.time'].volume)

    def test_a_start_is_not_counted(self):
        self.assertEqual({}, self._samples(
            _notification('container.create.start')))

    def test_an_update_is_not_counted(self):
        """Sent for a record not yet placed, or that never started too."""
        self.assertEqual({}, self._samples(
            _notification('container.update', status='Creating',
                          host=None)))
        self.assertEqual({}, self._samples(
            _notification('capsule.update', container_type='capsule',
                          status='Error')))

    def test_a_resize_gives_what_is_held_after(self):
        found = self._samples(_notification('container.resize.end',
                                            memory='1024'))

        self.assertEqual(1024, float(found['container.memory'].volume))

    def test_a_capsule_is_its_own(self):
        found = self._samples(_notification(
            'capsule.create.end', container_type='capsule',
            allocation_basis='max(...)'))

        self.assertEqual({'capsule.cpu', 'capsule.memory', 'capsule.disk'},
                         set(found))
        self.assertEqual('max(...)', found['capsule.cpu'].resource_metadata[
            'allocation_basis'])

    def test_a_capsules_container_is_not_a_capsule(self):
        """capsule.container.* must not be read as capsule.*."""
        self.assertEqual({}, self._samples(_notification(
            'capsule.container.create.end',
            container_type='capsule_container', capsule_uuid='cap-1')))


class ShippedEventDefinitionsTest(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.converter = converter.setup_events(_conf(), mock.Mock())

    def _traits(self, notification):
        event = self.converter.to_event('info', notification)
        return {t.name: t.value for t in event.traits}

    def test_a_container_event_carries_what_gnocchi_needs(self):
        traits = self._traits(_notification('container.delete.end'))

        for name in ('resource_id', 'project_id', 'user_id', 'name', 'host',
                     'image', 'runtime', 'container_type', 'status',
                     'occurred_at'):
            self.assertIn(name, traits)
        self.assertEqual('web', traits['server_group'])

    def test_a_capsule_event_carries_its_allocation_basis(self):
        traits = self._traits(_notification(
            'capsule.delete.end', container_type='capsule',
            allocation_basis='max(...)'))

        self.assertEqual('max(...)', traits['allocation_basis'])

    def test_a_capsules_container_is_not_read_as_a_capsule(self):
        event = self.converter.to_event('info', _notification(
            'capsule.container.delete.end',
            container_type='capsule_container'))
        traits = {t.name for t in (event.traits if event else [])}

        self.assertNotIn('allocation_basis', traits)


class ShippedGnocchiResourcesTest(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        with open('ceilometer/publisher/data/gnocchi_resources.yaml') as f:
            self.resources = {r['resource_type']: r for r in
                              yaml.safe_load(f)['resources']}

    def test_every_polled_meter_has_a_home(self):
        for kind in ('container', 'capsule'):
            metrics = self.resources['zun_' + kind]['metrics']
            for meter in zun.METERS:
                self.assertIn('%s.%s' % (zun.KINDS[kind]['prefix'], meter),
                              metrics)

    def test_a_resource_starts_and_ends_with_its_events(self):
        self.assertEqual(
            ('container.create.end', 'container.delete.end'),
            (self.resources['zun_container']['event_create'],
             self.resources['zun_container']['event_delete']))

    def test_capsule_containers_are_not_defined_by_default(self):
        self.assertNotIn('zun_capsule_container', self.resources)
