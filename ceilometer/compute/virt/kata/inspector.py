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
"""Implementation of Inspector abstraction for Kata Containers.

A Nova instance of the Kata compute driver (openstack-nova-kata) is a Kata
sandbox. Its counters come from the host's nova-kata-runtime gateway, over
its Unix socket, read-only: each vCPU thread's time, each port's host
interface from the guest's side, each Cinder disk's host block device, and
the guest workload's memory. The samples are the libvirt inspector's meters
of an instance (cpu, memory.usage, network.*, disk.device.*), named as
libvirt names them, so nothing downstream tells the hypervisors apart.

The gateway also reports each vCPU's wait for a CPU (cpu_delay), the VMM
process's resident memory (memory.resident) and each disk's size
(disk.device.capacity); a file root's allocation on the host's filesystem
(disk.device.allocation and usage) too, an RBD image's not, as it would
take a walk of its objects. It has no counter for disk latency nor for
memory swap.
"""

import http.client
import json
import socket
import threading
import time
from urllib import parse

from oslo_config import cfg
from oslo_log import log as logging

from ceilometer.compute.virt import inspector as virt_inspector

LOG = logging.getLogger(__name__)

OPTS = [
    cfg.StrOpt('kata_runtime_socket',
               default='/run/nova-kata/runtime.sock',
               help='Unix socket of the host\'s nova-kata-runtime gateway, '
                    'as [kata] runtime_socket of its nova-compute.'),
    cfg.FloatOpt('kata_runtime_timeout',
                 default=30.0,
                 min=1.0,
                 help='Seconds to wait for a read of the Kata gateway.'),
]

# The gateway API version this inspector reads. The gateway is installed
# with its compute driver, not with Ceilometer: a gateway of another
# version is refused, never read on a guess.
PROTOCOL_VERSION = 1

# The pollsters of one cycle each ask for the same instance: one read of
# the gateway serves them.
CACHE_SECONDS = 5.0
MIB = 1024 ** 2

# libvirt's domain states, which the meters carry: a sandbox that does not
# run is shut off, as a libvirt domain that does not run is.
POWER_STATE = {'running': 1, 'paused': 3, 'pausing': 3}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _NotFound(Exception):
    pass


class KataInspector(virt_inspector.Inspector):
    """Read cumulative instance counters from the host's Kata gateway."""

    def __init__(self, conf):
        super().__init__(conf)
        self._socket = conf.kata_runtime_socket
        self._timeout = conf.kata_runtime_timeout
        self._lock = threading.Lock()
        self._inventory = (0.0, None)
        self._diagnostics = {}

    def _get(self, path):
        connection = _UnixHTTPConnection(self._socket, self._timeout)
        try:
            connection.request('GET', path,
                               headers={'Accept': 'application/json'})
            response = connection.getresponse()
            status, body = response.status, response.read()
        except (OSError, http.client.HTTPException) as e:
            raise virt_inspector.InspectorException(
                'cannot read the Kata gateway at %s: %s' % (self._socket, e))
        finally:
            connection.close()
        if status == 404:
            raise _NotFound(path)
        try:
            payload = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            payload = None
        if not 200 <= status < 300 or not isinstance(payload, dict):
            raise virt_inspector.InspectorException(
                'the Kata gateway answered %s to GET %s' % (status, path))
        if payload.get('protocol_version') != PROTOCOL_VERSION:
            raise virt_inspector.InspectorException(
                'the Kata gateway speaks protocol %r, not %d' % (
                    payload.get('protocol_version'), PROTOCOL_VERSION))
        return payload

    def _generation(self, uuid):
        """The generation of the instance's own container on this host."""
        with self._lock:
            stamp, items = self._inventory
            if items is None or time.monotonic() - stamp > CACHE_SECONDS:
                try:
                    items = self._get('/v1/inventory')['items']
                except _NotFound:
                    raise virt_inspector.InspectorException(
                        'the Kata gateway has no inventory')
                if not isinstance(items, list):
                    raise virt_inspector.InspectorException(
                        'the Kata gateway sent an invalid inventory')
                self._inventory = (time.monotonic(), items)
        # The instance's own container, not a rescue container of it.
        own = [item.get('generation') for item in items
               if isinstance(item, dict) and
               item.get('instance_uuid') == uuid and
               item.get('container_id') == 'nova-' + uuid]
        if not own:
            raise virt_inspector.InstanceNotFoundException(
                'no Kata container of instance %s on this host' % uuid)
        if len(own) > 1 or not isinstance(own[0], str):
            raise virt_inspector.InspectorException(
                'the Kata gateway lists instance %s ambiguously' % uuid)
        return own[0]

    def _read(self, instance):
        uuid = instance.id
        with self._lock:
            cached = self._diagnostics.get(uuid)
            if cached and time.monotonic() - cached[0] <= CACHE_SECONDS:
                return cached[1]
        generation = self._generation(uuid)
        try:
            data = self._get('/v1/instances/%s/diagnostics?generation=%s' % (
                parse.quote(uuid, safe=''), parse.quote(generation, safe='')))
        except _NotFound:
            raise virt_inspector.InstanceNotFoundException(
                'no Kata container of instance %s on this host' % uuid)
        if (data.get('instance_uuid') != uuid or
                data.get('generation') != generation):
            raise virt_inspector.InspectorException(
                'the Kata gateway answered for another instance than %s' %
                uuid)
        with self._lock:
            now = time.monotonic()
            self._diagnostics = {
                key: value for key, value in self._diagnostics.items()
                if now - value[0] <= CACHE_SECONDS}
            self._diagnostics[uuid] = (now, data)
        return data

    def _running(self, instance):
        data = self._read(instance)
        if data.get('task_status') not in POWER_STATE:
            raise virt_inspector.InstanceShutOffException(
                'instance %s is not running' % instance.id)
        return data

    def inspect_instance(self, instance, duration=None):
        data = self._running(instance)
        flavor = getattr(instance, 'flavor', None) or {}
        vcpus = data.get('vcpus') or []
        cpu_time = sum(vcpu['time_ns'] for vcpu in vcpus) if vcpus else None
        # A gateway without the field reports no delay, not a zero one.
        delays = [vcpu['delay_ns'] for vcpu in vcpus if 'delay_ns' in vcpu]
        cpu_delay = sum(delays) if vcpus and len(delays) == len(vcpus) \
            else None
        memory = flavor.get('ram')
        used = data.get('memory_usage_bytes')
        resident = data.get('memory_resident_bytes')
        return virt_inspector.InstanceStats(
            power_state=POWER_STATE[data['task_status']],
            cpu_number=len(vcpus) or flavor.get('vcpus'),
            cpu_time=cpu_time,
            cpu_delay=cpu_delay,
            memory_actual=memory,
            memory_available=memory,
            memory_usage=used / MIB if used is not None else None,
            memory_resident=resident / MIB if resident is not None else None,
        )

    def inspect_vnics(self, instance, duration):
        data = self._running(instance)
        for nic in data.get('nics') or []:
            yield virt_inspector.InterfaceStats(
                # The host interface, as libvirt names a vNIC by its tap.
                name='tap' + nic['port_id'].replace('-', '')[:11],
                mac=nic['mac_address'].lower(),
                fref=None,
                parameters={},
                rx_bytes=nic['rx_bytes'],
                tx_bytes=nic['tx_bytes'],
                rx_packets=nic['rx_packets'],
                tx_packets=nic['tx_packets'],
                rx_drop=nic['rx_drop'],
                tx_drop=nic['tx_drop'],
                rx_errors=nic['rx_errors'],
                tx_errors=nic['tx_errors'],
                rx_bytes_delta=None,
                tx_bytes_delta=None,
            )

    @staticmethod
    def _device(instance, disk):
        """A disk's name as libvirt names it: the root's, a volume's."""
        if disk.get('root'):
            device = getattr(instance, 'OS-EXT-SRV-ATTR:root_device_name',
                             None) or '/dev/vda'
        else:
            device = disk.get('device') or disk['volume_uuid']
        return device.rsplit('/', 1)[-1]

    def inspect_disk_info(self, instance, duration):
        data = self._running(instance)
        for disk in data.get('disks') or []:
            capacity = disk.get('capacity_bytes')
            if not capacity:
                continue
            allocation = disk.get('allocation_bytes')
            yield virt_inspector.DiskInfo(
                device=self._device(instance, disk),
                capacity=capacity,
                allocation=allocation,
                physical=allocation,
            )

    def inspect_disks(self, instance, duration):
        data = self._running(instance)
        for disk in data.get('disks') or []:
            yield virt_inspector.DiskStats(
                device=self._device(instance, disk),
                read_bytes=disk['read_bytes'],
                read_requests=disk['read_requests'],
                write_bytes=disk['write_bytes'],
                write_requests=disk['write_requests'],
                errors=-1,
                wr_total_times=None,
                rd_total_times=None,
            )
