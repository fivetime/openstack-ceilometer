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
"""Tests for the Incus inspector."""
from unittest import mock

import requests

from ceilometer.compute.virt.incus import inspector as incus_inspector
from ceilometer.compute.virt import inspector as virt_inspector
from ceilometer import service
from ceilometer.tests import base

# Trimmed from a real Incus 7 /1.0/metrics response.
METRICS = '''\
# HELP incus_cpu_seconds_total The total number of CPU time used in seconds.
# TYPE incus_cpu_seconds_total counter
incus_cpu_seconds_total{cpu="0",mode="system",name="instance-00000001",\
project="default",type="container"} 14.25
incus_cpu_seconds_total{cpu="0",mode="user",name="instance-00000001",\
project="default",type="container"} 20.5
incus_cpu_seconds_total{cpu="1",mode="user",name="instance-00000001",\
project="default",type="container"} 5.25
incus_cpu_seconds_total{cpu="0",mode="user",name="instance-00000002",\
project="default",type="container"} 99
# HELP incus_cpu_effective_total The total number of effective CPUs.
# TYPE incus_cpu_effective_total gauge
incus_cpu_effective_total{name="instance-00000001",project="default",\
type="container"} 2
# HELP incus_disk_read_bytes_total The total number of bytes read.
# TYPE incus_disk_read_bytes_total counter
incus_disk_read_bytes_total{device="rbd5",name="instance-00000001",\
project="default",type="container"} 4.4785664e+08
# HELP incus_memory_MemAvailable_bytes The amount of available memory.
# TYPE incus_memory_MemAvailable_bytes gauge
incus_memory_MemAvailable_bytes{name="instance-00000001",project="default",\
type="container"} 1.073741824e+09
# HELP incus_memory_MemTotal_bytes The amount of used memory.
# TYPE incus_memory_MemTotal_bytes gauge
incus_memory_MemTotal_bytes{name="instance-00000001",project="default",\
type="container"} 4.294967296e+09
incus_memory_MemTotal_bytes{name="vm1",project="default",\
type="virtual-machine"} 1.073741824e+09
# HELP incus_memory_RSS_bytes The amount of anonymous and swap cache memory.
# TYPE incus_memory_RSS_bytes gauge
incus_memory_RSS_bytes{name="instance-00000001",project="default",\
type="container"} 2.147483648e+09
# HELP incus_network_receive_bytes_total The amount of received bytes.
# TYPE incus_network_receive_bytes_total counter
incus_network_receive_bytes_total{device="lo",name="instance-00000001",\
project="default",type="container"} 7
incus_network_receive_bytes_total{device="nicffd9ff0c7777",\
name="instance-00000001",project="default",type="container"} 1000
incus_network_receive_bytes_total{device="eth9",name="instance-00000001",\
project="default",type="container"} 3
# HELP incus_network_transmit_bytes_total The amount of transmitted bytes.
# TYPE incus_network_transmit_bytes_total counter
incus_network_transmit_bytes_total{device="nicffd9ff0c7777",\
name="instance-00000001",project="default",type="container"} 2000
# HELP incus_network_receive_packets_total The amount of received packets.
# TYPE incus_network_receive_packets_total counter
incus_network_receive_packets_total{device="nicffd9ff0c7777",\
name="instance-00000001",project="default",type="container"} 10
# HELP incus_network_transmit_drop_total The amount of transmitted drops.
# TYPE incus_network_transmit_drop_total counter
incus_network_transmit_drop_total{device="nicffd9ff0c7777",\
name="instance-00000001",project="default",type="container"} 1
# HELP incus_uptime_seconds The daemon uptime in seconds.
# TYPE incus_uptime_seconds gauge
incus_uptime_seconds 5000
'''


class Instance:
    id = 'ff58e738-12f4-4c58-acde-77617b68da56'
    flavor = {'vcpus': 2, 'ram': 4096}

    def __init__(self, name='instance-00000001', vm_state='active'):
        setattr(self, 'OS-EXT-SRV-ATTR:instance_name', name)
        setattr(self, 'OS-EXT-STS:vm_state', vm_state)


class TestIncusInspector(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.conf = service.prepare_service([], [])
        self.conf.set_override('metrics_url', 'https://192.0.2.10:8443/',
                               group='incus')
        self.conf.set_override('client_cert', '/etc/ceilometer/incus.crt',
                               group='incus')
        self.conf.set_override('client_key', '/etc/ceilometer/incus.key',
                               group='incus')
        self.conf.set_override('server_cert', '/etc/ceilometer/incusd.crt',
                               group='incus')
        self.inspector = incus_inspector.IncusInspector(self.conf)
        self.response = mock.Mock(text=METRICS)
        self.get = mock.patch.object(self.inspector.session, 'get',
                                     return_value=self.response).start()

    def test_requires_metrics_url(self):
        self.conf.clear_override('metrics_url', group='incus')
        self.assertRaises(virt_inspector.InspectorException,
                          incus_inspector.IncusInspector, self.conf)

    def test_session_uses_metrics_certificate(self):
        self.assertEqual(('/etc/ceilometer/incus.crt',
                          '/etc/ceilometer/incus.key'),
                         self.inspector.session.cert)
        self.assertEqual('/etc/ceilometer/incusd.crt',
                         self.inspector.session.verify)

    def test_inspect_instance(self):
        stats = self.inspector.inspect_instance(Instance())
        self.get.assert_called_once_with(
            'https://192.0.2.10:8443/1.0/metrics',
            params={'project': 'default'}, timeout=30.0)
        self.assertEqual(1, stats.power_state)
        self.assertEqual(2, stats.cpu_number)
        self.assertEqual(40000000000, stats.cpu_time)
        self.assertEqual(4096, stats.memory_actual)
        self.assertEqual(1024, stats.memory_available)
        self.assertEqual(3072, stats.memory_usage)
        self.assertEqual(2048, stats.memory_resident)
        self.assertIsNone(stats.memory_swap_in)
        self.assertIsNone(stats.memory_swap_out)

    def test_resident_memory_on_cgroup_v2(self):
        self.response.text = METRICS.replace('incus_memory_RSS_bytes',
                                             'incus_memory_Active_anon_bytes')
        self.response.text += (
            '# HELP incus_memory_Inactive_anon_bytes Inactive anon.\n'
            '# TYPE incus_memory_Inactive_anon_bytes gauge\n'
            'incus_memory_Inactive_anon_bytes{name="instance-00000001",'
            'project="default",type="container"} 1.048576e+08\n')
        stats = self.inspector.inspect_instance(Instance())
        self.assertEqual(2148, stats.memory_resident)

    def test_cpu_number_falls_back_to_effective_cpus(self):
        instance = Instance()
        instance.flavor = {}
        self.assertEqual(
            2, self.inspector.inspect_instance(instance).cpu_number)

    def test_one_request_serves_a_polling_cycle(self):
        self.inspector.inspect_instance(Instance())
        list(self.inspector.inspect_vnics(Instance()))
        self.inspector.inspect_instance(Instance('instance-00000002'))
        self.assertEqual(1, self.get.call_count)

    @mock.patch.object(incus_inspector.time, 'monotonic')
    def test_metrics_are_read_again_after_ttl(self, monotonic):
        monotonic.return_value = 100.0
        self.inspector.inspect_instance(Instance())
        monotonic.return_value = 100.0 + incus_inspector.METRICS_TTL + 1
        self.inspector.inspect_instance(Instance())
        self.assertEqual(2, self.get.call_count)

    def test_inspect_vnics(self):
        nics = list(self.inspector.inspect_vnics(Instance()))
        self.assertEqual(['eth9', 'tapffd9ff0c-77'],
                         [nic.name for nic in nics])
        tap = nics[1]
        self.assertEqual(1000, tap.rx_bytes)
        self.assertEqual(2000, tap.tx_bytes)
        self.assertEqual(10, tap.rx_packets)
        self.assertEqual(1, tap.tx_drop)
        self.assertIsNone(tap.tx_packets)
        self.assertIsNone(tap.fref)
        self.assertIsNone(tap.mac)

    def test_stopped_instance_is_shut_off(self):
        with mock.patch.object(incus_inspector.LOG, 'warning') as warning:
            self.assertRaises(virt_inspector.InstanceShutOffException,
                              self.inspector.inspect_instance,
                              Instance('instance-00000009', 'stopped'))
        warning.assert_not_called()

    def test_active_instance_without_metrics_is_logged(self):
        with mock.patch.object(incus_inspector.LOG, 'warning') as warning:
            self.assertRaises(virt_inspector.InstanceShutOffException,
                              self.inspector.inspect_instance,
                              Instance('instance-00000009'))
        warning.assert_called_once()

    def test_virtual_machines_are_ignored(self):
        self.assertRaises(virt_inspector.InstanceShutOffException,
                          self.inspector.inspect_instance, Instance('vm1'))

    def test_instance_without_name(self):
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          self.inspector.inspect_instance, Instance(None))
        self.get.assert_not_called()

    def test_request_failure(self):
        self.get.side_effect = requests.ConnectionError('refused')
        self.assertRaises(virt_inspector.InspectorException,
                          self.inspector.inspect_instance, Instance())

    def test_http_error(self):
        self.response.raise_for_status.side_effect = requests.HTTPError(
            '403 Forbidden')
        self.assertRaises(virt_inspector.InspectorException,
                          self.inspector.inspect_instance, Instance())
