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
"""Implementation of Inspector abstraction for Incus system containers.

The inspector reads the host's Incus metrics endpoint (``/1.0/metrics``)
and nothing else. A certificate of type ``metrics`` is trusted for that
endpoint only, so the agent never holds the administrative access that the
Incus unix socket or an ordinary client certificate would give it.
"""

import re
import threading
import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units
from prometheus_client import parser
import requests

from ceilometer.compute.pollsters import util
from ceilometer.compute.virt import inspector as virt_inspector

LOG = logging.getLogger(__name__)

OPTS = [
    cfg.URIOpt('metrics_url',
               schemes=['https'],
               help='HTTPS URL of the local Incus server, for example '
                    'https://192.0.2.10:8443. Its /1.0/metrics endpoint is '
                    'read for every instance on this host. Point it at '
                    'core.metrics_address when that is set, otherwise at '
                    'core.https_address.'),
    cfg.StrOpt('project',
               default='default',
               help='Incus project that holds the Nova instances.'),
    cfg.StrOpt('client_cert',
               help='Client certificate trusted by Incus with type '
                    '"metrics".'),
    cfg.StrOpt('client_key',
               help='Private key of client_cert.'),
    cfg.StrOpt('server_cert',
               help='Certificate or CA bundle that verifies the Incus '
                    'server. Leave unset to use the system CA store.'),
    cfg.FloatOpt('timeout',
                 default=30.0,
                 min=1.0,
                 help='Timeout in seconds of one metrics request.'),
]

# Incus itself caches the metrics of a project for 8 seconds. Every pollster
# of a polling cycle reads the same snapshot for all instances, so one
# request serves them all rather than one per instance and pollster.
METRICS_TTL = 10.0

# libvirt's VIR_DOMAIN_RUNNING. Incus publishes metrics only for running
# instances, so every instance found in them is running.
POWER_STATE_RUNNING = 1

# The guest name of a Nova NIC is "nic" and the first 12 hex digits of its
# port UUID; the host side, which libvirt names its vNIC after, is "tap" and
# the first 11 characters of the UUID.
GUEST_NIC_RE = re.compile(r'^nic([0-9a-f]{8})([0-9a-f]{2})[0-9a-f]{2}$')


def _tap_name(device):
    match = GUEST_NIC_RE.match(device)
    if match is None:
        return device
    return 'tap%s-%s' % match.groups()


class _Instance:
    """The metrics of one running instance."""

    def __init__(self):
        self.cpu_seconds = 0.0
        self.cpus = None
        self.memory = {}
        self.nics = {}


class IncusInspector(virt_inspector.Inspector):

    def __init__(self, conf):
        super().__init__(conf)
        if not conf.incus.metrics_url:
            raise virt_inspector.InspectorException(
                '[incus] metrics_url is not set')
        self.url = conf.incus.metrics_url.rstrip('/') + '/1.0/metrics'
        self.session = requests.Session()
        if conf.incus.client_cert:
            self.session.cert = (conf.incus.client_cert,
                                 conf.incus.client_key)
        if conf.incus.server_cert:
            self.session.verify = conf.incus.server_cert
        self._lock = threading.Lock()
        self._fetched = None
        self._instances = {}

    def _read_metrics(self):
        try:
            response = self.session.get(
                self.url, params={'project': self.conf.incus.project},
                timeout=self.conf.incus.timeout)
            response.raise_for_status()
        except requests.RequestException as e:
            raise virt_inspector.InspectorException(
                'Cannot read %s: %s' % (self.url, e))

        instances = {}
        for family in parser.text_string_to_metric_families(response.text):
            for sample in family.samples:
                name, labels, value = sample.name, sample.labels, sample.value
                if labels.get('type') != 'container' or 'name' not in labels:
                    continue
                inst = instances.setdefault(labels['name'], _Instance())
                if name == 'incus_cpu_seconds_total':
                    inst.cpu_seconds += value
                elif name == 'incus_cpu_effective_total':
                    inst.cpus = int(value)
                elif name.startswith('incus_memory_'):
                    inst.memory[name] = value
                elif name.startswith('incus_network_'):
                    nic = inst.nics.setdefault(labels.get('device'), {})
                    nic[name] = int(value)
        return instances

    def _get_instance(self, instance):
        name = util.instance_name(instance)
        if not name:
            raise virt_inspector.InstanceNotFoundException(
                'Nova did not expose the name of instance %s' % instance.id)
        with self._lock:
            if (self._fetched is None or
                    time.monotonic() - self._fetched > METRICS_TTL):
                self._instances = self._read_metrics()
                self._fetched = time.monotonic()
            metrics = self._instances.get(name)
        if metrics is None:
            if getattr(instance, 'OS-EXT-STS:vm_state', None) == 'active':
                # An active instance that Incus has no metrics for would
                # otherwise go unmetered without a trace.
                LOG.warning('Incus has no metrics for active instance '
                            '%(id)s (%(name)s)',
                            {'id': instance.id, 'name': name})
            raise virt_inspector.InstanceShutOffException(
                'Incus instance %s is not running' % name)
        return metrics

    def inspect_instance(self, instance, duration=None):
        metrics = self._get_instance(instance)
        memory = metrics.memory
        total = memory.get('incus_memory_MemTotal_bytes')
        available = memory.get('incus_memory_MemAvailable_bytes')
        rss = memory.get('incus_memory_RSS_bytes')
        if rss is None and 'incus_memory_Active_anon_bytes' in memory:
            # Incus reads RSS from cgroup v1 only; on v2 it is the anonymous
            # memory, active and inactive.
            rss = (memory['incus_memory_Active_anon_bytes'] +
                   memory.get('incus_memory_Inactive_anon_bytes', 0))
        flavor = getattr(instance, 'flavor', None) or {}
        return virt_inspector.InstanceStats(
            power_state=POWER_STATE_RUNNING,
            # The Flavor's vCPUs, which is what libvirt reports and what the
            # instance pays for; limits.cpu holds the same number.
            cpu_number=flavor.get('vcpus', metrics.cpus),
            cpu_time=int(metrics.cpu_seconds * units.G),
            memory_actual=total / units.Mi if total is not None else None,
            memory_available=(available / units.Mi
                              if available is not None else None),
            memory_usage=((total - available) / units.Mi
                          if total is not None and available is not None
                          else None),
            memory_resident=rss / units.Mi if rss is not None else None,
        )

    def inspect_vnics(self, instance, duration=None):
        metrics = self._get_instance(instance)
        for device, counters in sorted(metrics.nics.items()):
            if device == 'lo':
                continue
            # Incus counts in the guest, as libvirt reports a tap: what the
            # guest received is incoming.
            yield virt_inspector.InterfaceStats(
                name=_tap_name(device),
                mac=None,
                fref=None,
                parameters=None,
                rx_bytes=counters.get('incus_network_receive_bytes_total'),
                tx_bytes=counters.get('incus_network_transmit_bytes_total'),
                rx_packets=counters.get(
                    'incus_network_receive_packets_total'),
                tx_packets=counters.get(
                    'incus_network_transmit_packets_total'),
                rx_drop=counters.get('incus_network_receive_drop_total'),
                tx_drop=counters.get('incus_network_transmit_drop_total'),
                rx_errors=counters.get('incus_network_receive_errs_total'),
                tx_errors=counters.get('incus_network_transmit_errs_total'),
                rx_bytes_delta=None,
                tx_bytes_delta=None,
            )
