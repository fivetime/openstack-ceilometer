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

import http.server
import json
import os
import socketserver
import tempfile
import threading
from types import SimpleNamespace
import unittest

from ceilometer.compute.virt import inspector as virt_inspector
from ceilometer.compute.virt.kata import inspector as kata_inspector
from ceilometer import service
from ceilometer.tests import base

UUID = '30d36e7b-5684-4f7a-90c4-69ed3b5f6b31'
GENERATION = '50231917-1da2-45ce-86af-f15c1b99e9bb'
PORT = '8cb05632-93af-489a-ace3-ba1afedb5559'


def inventory(*items):
    return {'protocol_version': 1, 'integrity_errors': [],
            'items': list(items)}


def item(uuid=UUID, container=None, generation=GENERATION):
    return {'instance_uuid': uuid, 'container_id': container or 'nova-' + uuid,
            'generation': generation}


def diagnostics(task_status='running'):
    running = task_status == 'running'
    return {
        'protocol_version': 1, 'instance_uuid': UUID,
        'generation': GENERATION, 'task_status': task_status,
        'memory_usage_bytes': 512 * 1024 ** 2 if running else None,
        'memory_resident_bytes': 300 * 1024 ** 2 if running else None,
        'vcpus': [{'id': 0, 'time_ns': 1000, 'delay_ns': 7},
                  {'id': 1, 'time_ns': 2000, 'delay_ns': 8}]
        if running else [],
        'nics': [{'port_id': PORT, 'mac_address': 'FA:16:3E:00:00:01',
                  'rx_bytes': 1, 'rx_packets': 2, 'rx_errors': 3,
                  'rx_drop': 4, 'tx_bytes': 5, 'tx_packets': 6,
                  'tx_errors': 7, 'tx_drop': 8}] if running else [],
        # A file root, with its allocation; a volume, a block device.
        'disks': [{'root': True, 'read_bytes': 10, 'read_requests': 1,
                   'write_bytes': 20, 'write_requests': 2,
                   'capacity_bytes': 10 * 1024 ** 3,
                   'allocation_bytes': 3 * 1024 ** 3},
                  {'root': False, 'volume_uuid': 'volume-1',
                   'device': '/dev/sdb', 'read_bytes': 30,
                   'read_requests': 3, 'write_bytes': 40,
                   'write_requests': 4,
                   'capacity_bytes': 20 * 1024 ** 3}] if running else [],
    }


class FakeGateway:
    """Answers the inspector's GETs from a table, and counts them."""

    def __init__(self):
        self.calls = []
        self.answers = {
            '/v1/inventory': (200, inventory(
                item(container='nova-' + GENERATION), item(),
                item('other'))),
            '/v1/instances/%s/diagnostics?generation=%s' % (
                UUID, GENERATION): (200, diagnostics()),
        }

    def __call__(self, path):
        self.calls.append(path)
        status, body = self.answers.get(path, (404, {}))
        if status == 404:
            raise kata_inspector._NotFound(path)
        return body


class TestKataInspector(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        self.conf = service.prepare_service([], [])
        self.inspector = kata_inspector.KataInspector(self.conf)
        self.gateway = FakeGateway()
        self.inspector._get = self.gateway
        self.instance = SimpleNamespace(
            id=UUID, flavor={'vcpus': 2, 'ram': 2048},
            **{'OS-EXT-SRV-ATTR:root_device_name': '/dev/sda'})

    def test_instance_stats_are_libvirts(self):
        stats = self.inspector.inspect_instance(self.instance, None)
        self.assertEqual(1, stats.power_state)
        self.assertEqual(2, stats.cpu_number)
        self.assertEqual(3000, stats.cpu_time)
        self.assertEqual(2048, stats.memory_actual)
        self.assertEqual(512, stats.memory_usage)
        self.assertEqual(300, stats.memory_resident)
        # The instance's own container, not a rescue one.
        self.assertEqual(
            '/v1/instances/%s/diagnostics?generation=%s' % (UUID, GENERATION),
            self.gateway.calls[-1])

    def test_vnics_and_disks_are_named_as_libvirt_names_them(self):
        vnics = list(self.inspector.inspect_vnics(self.instance, None))
        self.assertEqual(['tap' + PORT.replace('-', '')[:11]],
                         [v.name for v in vnics])
        self.assertEqual('fa:16:3e:00:00:01', vnics[0].mac)
        self.assertEqual((1, 5, 2, 6), (vnics[0].rx_bytes, vnics[0].tx_bytes,
                                        vnics[0].rx_packets,
                                        vnics[0].tx_packets))
        disks = list(self.inspector.inspect_disks(self.instance, None))
        self.assertEqual([('sda', 20), ('sdb', 40)],
                         [(d.device, d.write_bytes) for d in disks])

    def test_disk_info_has_the_size_and_a_file_roots_allocation(self):
        info = list(self.inspector.inspect_disk_info(self.instance, None))
        self.assertEqual(
            [('sda', 10 * 1024 ** 3, 3 * 1024 ** 3, 3 * 1024 ** 3),
             ('sdb', 20 * 1024 ** 3, None, None)],
            [(d.device, d.capacity, d.allocation, d.physical) for d in info])

    def test_a_gateway_without_the_new_counters_reports_none(self):
        data = diagnostics()
        del data['memory_resident_bytes']
        for disk in data['disks']:
            disk.pop('capacity_bytes')
            disk.pop('allocation_bytes', None)
        self.gateway.answers[
            '/v1/instances/%s/diagnostics?generation=%s' % (
                UUID, GENERATION)] = (200, data)
        stats = self.inspector.inspect_instance(self.instance, None)
        self.assertIsNone(stats.memory_resident)
        self.assertEqual(
            [], list(self.inspector.inspect_disk_info(self.instance, None)))

    def test_one_read_serves_the_pollsters_of_a_cycle(self):
        self.inspector.inspect_instance(self.instance, None)
        list(self.inspector.inspect_vnics(self.instance, None))
        list(self.inspector.inspect_disks(self.instance, None))
        self.assertEqual(2, len(self.gateway.calls))

    def test_a_stopped_instance_is_shut_off(self):
        self.gateway.answers[
            '/v1/instances/%s/diagnostics?generation=%s' % (
                UUID, GENERATION)] = (200, diagnostics('stopped'))
        self.assertRaises(virt_inspector.InstanceShutOffException,
                          self.inspector.inspect_instance, self.instance,
                          None)

    def test_an_instance_of_no_container_here_is_not_found(self):
        self.gateway.answers['/v1/inventory'] = (200, inventory())
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          self.inspector.inspect_instance, self.instance,
                          None)

    def test_a_container_gone_since_the_inventory_is_not_found(self):
        del self.gateway.answers[
            '/v1/instances/%s/diagnostics?generation=%s' % (UUID, GENERATION)]
        self.assertRaises(virt_inspector.InstanceNotFoundException,
                          self.inspector.inspect_instance, self.instance,
                          None)

    def test_two_own_containers_are_not_guessed_between(self):
        self.gateway.answers['/v1/inventory'] = (200, inventory(
            item(), item(generation='another')))
        self.assertRaises(virt_inspector.InspectorException,
                          self.inspector.inspect_instance, self.instance,
                          None)

    def test_an_answer_for_another_generation_is_refused(self):
        other = diagnostics()
        other['generation'] = 'another'
        self.gateway.answers[
            '/v1/instances/%s/diagnostics?generation=%s' % (
                UUID, GENERATION)] = (200, other)
        self.assertRaises(virt_inspector.InspectorException,
                          self.inspector.inspect_instance, self.instance,
                          None)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = self.server.answers[self.path]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def address_string(self):
        return 'unix'

    def log_message(self, *args):
        pass


@unittest.skipUnless(hasattr(socketserver, 'UnixStreamServer'),
                     'no Unix sockets')
class TestKataGatewaySocket(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, directory)
        self.path = os.path.join(directory, 'runtime.sock')
        self.server = socketserver.UnixStreamServer(self.path, _Handler)
        self.addCleanup(os.unlink, self.path)
        self.addCleanup(self.server.server_close)
        self.server.answers = {}
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.conf = service.prepare_service([], [])
        self.conf.set_override('kata_runtime_socket', self.path)
        self.inspector = kata_inspector.KataInspector(self.conf)

    def test_reads_the_gateway_over_its_socket(self):
        self.server.answers['/v1/inventory'] = (200, inventory(item()))
        self.assertEqual(GENERATION, self.inspector._generation(UUID))

    def test_another_protocol_is_refused(self):
        body = inventory(item())
        body['protocol_version'] = 2
        self.server.answers['/v1/inventory'] = (200, body)
        self.assertRaises(virt_inspector.InspectorException,
                          self.inspector._generation, UUID)

    def test_an_error_answer_is_an_inspector_error(self):
        self.server.answers['/v1/inventory'] = (
            503, {'error': {'code': 'unavailable'}})
        self.assertRaises(virt_inspector.InspectorException,
                          self.inspector._generation, UUID)

    def test_no_gateway_is_an_inspector_error(self):
        self.conf.set_override('kata_runtime_socket', self.path + '.absent')
        self.assertRaises(virt_inspector.InspectorException,
                          kata_inspector.KataInspector(self.conf)._generation,
                          UUID)
