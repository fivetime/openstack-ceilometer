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
"""Zun hosts to poll, one per kind of object read from them.

Discovery hands out hosts rather than containers: Zun answers for a whole
host in one call, so a host is the unit a polling agent asks about and the
unit workload partitioning shares out -- a resource is partitioned by its
string, which is its host, so every kind read from one host lands on the
same agent.

A discovery parameter narrows what is read to objects carrying a label,
for an operator who polls a fine interval only for what autoscales:
``zun_containers://label=metering.autoscale``.
"""

from oslo_config import cfg

from ceilometer.polling import plugin_base
from ceilometer import zun_client

OPTS = [
    cfg.BoolOpt('meter_capsule_containers',
                default=False,
                help='Also poll the containers inside capsules (init '
                     'containers included). Off by default: a capsule is '
                     'read as a whole, and its containers\' allocations are '
                     'counted in the capsule\'s, so metering both is only '
                     'for a deployment that bills per container.'),
]


class HostToRead:
    """One host, which kind of object to read there, and an optional label.

    Partitioned by its string, which is the host alone.
    """

    def __init__(self, host, kind, label=None):
        self.host = host
        self.kind = kind
        self.label = label

    def __str__(self):
        return self.host

    def __repr__(self):
        return 'HostToRead(%r, %r, %r)' % (self.host, self.kind, self.label)

    def __eq__(self, other):
        return (isinstance(other, HostToRead) and
                (self.host, self.kind, self.label) ==
                (other.host, other.kind, other.label))

    def __hash__(self):
        return hash((self.host, self.kind, self.label))


def _label(param):
    if param and param.startswith('label='):
        return param[len('label='):] or None
    return None


class _ZunHostsDiscovery(plugin_base.DiscoveryBase):
    KEYSTONE_REQUIRED_FOR_SERVICE = 'zun'
    KIND = None

    def __init__(self, conf):
        super().__init__(conf)
        self.client = zun_client.Client(conf)

    def discover(self, manager, param=None):
        label = _label(param)
        return [HostToRead(host, self.KIND, label)
                for host in self.client.hosts()]


class ContainersDiscovery(_ZunHostsDiscovery):
    """Hosts to read standalone containers from."""
    KIND = 'container'


class CapsulesDiscovery(_ZunHostsDiscovery):
    """Hosts to read capsules, each as a whole, from."""
    KIND = 'capsule'


class CapsuleContainersDiscovery(_ZunHostsDiscovery):
    """Hosts to read capsules' containers from, when that is enabled."""
    KIND = 'capsule_container'

    def discover(self, manager, param=None):
        if not self.conf.zun.meter_capsule_containers:
            return []
        return super().discover(manager, param)
