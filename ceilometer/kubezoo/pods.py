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
"""Pollsters for kubezoo tenants' pods: their size, every polling cycle.

A pod is billed the way Fargate bills one, and the way a Nova instance is
billed by its flavor: at its size, used or not. The size is what the
scheduler set aside for it -- requests, which the policy layer keeps equal to
limits, plus the RuntimeClass overhead -- worked out by kubezoo-metering, so
these pollsters only report it:

- ``k8s.pod.cpu`` (vcpu), ``k8s.pod.memory`` (MB), ``k8s.pod.disk``
  (GB of ephemeral storage), on Gnocchi's ``k8s_pod``.

One point per pod per cycle, which CloudKitty sums per minute exactly as it
does Zun's capsule.* (see the release note). A pod appears only while it is
billed: from its first container's start until it is Succeeded, Failed, or
deleted with its containers finished. That matters beyond the bill: the
resource's ended_at is the pod's real end, and Gnocchi revives a resource
with a measure at or after its ended_at -- so nothing is ever sampled then.

A node that cannot be read costs only its own figures, with a warning naming
it; a pod listed as unavailable (its namespace has no project yet) is
skipped, and kubezoo-metering counts it.
"""

from oslo_log import log

from ceilometer.kubezoo import discovery
from ceilometer import kubezoo_client
from ceilometer.polling import plugin_base
from ceilometer import sample

LOG = log.getLogger(__name__)

#: meter: (unit, allocation key)
METERS = {
    'k8s.pod.cpu': ('vcpu', 'cpu'),
    'k8s.pod.memory': ('MB', 'memory_mb'),
    'k8s.pod.disk': ('GB', 'ephemeral_storage_gb'),
}

#: Copied from an entry to the sample's metadata, named as the event meters
#: name them, so that a resource's attributes are the same whichever wrote
#: them last. Every one is the platform's, never the tenant's: they may
#: decide a price (see kubezoo-metering's pkg/notify).
_METADATA_FIELDS = ('name', 'namespace', 'tenant_id', 'node', 'pool',
                    'runtime_class', 'qos_class', 'platform_workload',
                    'owner_kind', 'owner_name', 'started_at')


def metadata(conf, entry):
    meta = {k: entry.get(k) for k in _METADATA_FIELDS if k in entry}
    # A string, as the event trait makes it: the attribute is a string.
    if 'platform_workload' in meta:
        meta['platform_workload'] = str(bool(meta['platform_workload']))
    meta['status'] = entry.get('phase')
    meta['allocation_basis'] = (entry.get('allocation') or {}).get('basis')
    # The tenant's metering.* labels, prefix stripped, as user_metadata --
    # the same treatment, by the same code, as nova's instance metadata and
    # Zun's labels get. Metadata only: the tenant wrote them.
    sample.add_reserved_user_metadata(conf, entry.get('metering') or {}, meta)
    return meta


class _PodPollster(plugin_base.PollsterBase):
    METER = None

    def __init__(self, conf):
        super().__init__(conf)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = kubezoo_client.Client(self.conf)
        return self._client

    @property
    def default_discovery(self):
        return 'kubezoo_nodes'

    def _answer(self, manager, cache, resource):
        """The node's answer, read once per polling cycle for all meters."""
        key = ('kubezoo', resource.node)
        if key not in cache:
            cache[key] = self.client.node_pods(resource.node)
            discovery.heartbeat(manager, 'kubezoo.read.%s' % resource.node)
        return cache[key]

    def get_samples(self, manager, cache, resources):
        unit, key = METERS[self.METER]
        for resource in resources or []:
            try:
                answer = self._answer(manager, cache, resource)
            except Exception as exc:
                LOG.warning('Could not read kubezoo node %(node)s: %(err)s',
                            {'node': resource.node, 'err': exc})
                cache[('kubezoo', resource.node)] = {'results': []}
                continue
            for entry in answer.get('results') or []:
                volume = (entry.get('allocation') or {}).get(key)
                if volume is None or not entry.get('project_id'):
                    continue
                yield sample.Sample(
                    name=self.METER,
                    type=sample.TYPE_GAUGE,
                    unit=unit,
                    volume=volume,
                    user_id=None,
                    project_id=entry['project_id'],
                    resource_id=entry['pod_uid'],
                    # When kubezoo-metering read its cache, not when this
                    # agent asked.
                    timestamp=answer.get('sampled_at'),
                    resource_metadata=metadata(self.conf, entry))


class PodCpuPollster(_PodPollster):
    """k8s.pod.cpu: the pod's vCPUs."""
    METER = 'k8s.pod.cpu'


class PodMemoryPollster(_PodPollster):
    """k8s.pod.memory: the pod's memory, MB."""
    METER = 'k8s.pod.memory'


class PodDiskPollster(_PodPollster):
    """k8s.pod.disk: the pod's ephemeral storage, GB."""
    METER = 'k8s.pod.disk'
