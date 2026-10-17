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
"""Pollsters for Zun's containers and capsules.

Three kinds of object, three meter prefixes, three Gnocchi resource types,
none overlapping:

- ``container.*``: a standalone container (``zun_container``);
- ``capsule.*``: a capsule read as a whole, from its sandbox
  (``zun_capsule``);
- ``capsule.container.*``: a capsule's container, init containers included
  (``zun_capsule_container``). Its allocation is already counted in its
  capsule's, so it is off unless ``[zun] meter_capsule_containers``.

Each kind has the same meters: what was allocated (``cpu``, ``memory``,
``disk``) and what was used (``cpu.time``, ``memory.usage``, network and
disk bytes). One host is read once per polling cycle for each kind,
whichever of its pollsters asks first; the others reuse the answer.

A sample is timestamped with the time the node took the reading, not the
time it was asked for, so a rate worked out from two samples is right
however late either answer arrived. What the node could not read is not
sampled: an absent figure is not a zero.
"""

from oslo_log import log

from ceilometer.container import discovery
from ceilometer.polling import plugin_base
from ceilometer import sample
from ceilometer import zun_client

LOG = log.getLogger(__name__)

#: What each kind of pollster reads, and how its meters are named.
KINDS = {
    'container': {'prefix': 'container',
                  'discovery': 'zun_containers',
                  'types': ('container',)},
    'capsule': {'prefix': 'capsule',
                'discovery': 'zun_capsules',
                'types': ('capsule',)},
    'capsule_container': {'prefix': 'capsule.container',
                          'discovery': 'zun_capsule_containers',
                          'types': ('capsule_container',
                                    'capsule_init_container')},
}

_MIB = 1024 * 1024


def _alloc(key):
    return lambda entry: (entry.get('allocation') or {}).get(key)


def _cpu_time(entry):
    return ((entry.get('reading') or {}).get('cpu') or {}).get('total_ns')


def _memory_usage(entry):
    memory = (entry.get('reading') or {}).get('memory') or {}
    usage = memory.get('usage')
    if usage is None:
        return None
    # The working set: usage less the inactive page cache, as the kubelet
    # and cAdvisor count it. A runtime that reports the working set itself
    # gives no cache.
    return max(usage - (memory.get('cache') or 0), 0) / float(_MIB)


def _network(direction):
    def volume(entry):
        networks = (entry.get('reading') or {}).get('networks')
        if not networks:
            return None
        values = [n.get(direction) for n in networks.values()
                  if n.get(direction) is not None]
        return sum(values) if values else None
    return volume


def _blkio(key):
    return lambda entry: ((entry.get('reading') or {}).get('blkio') or
                          {}).get(key)


#: meter suffix: (sample type, unit, how to read the volume)
METERS = {
    'cpu': (sample.TYPE_GAUGE, 'vcpu', _alloc('cpu')),
    'memory': (sample.TYPE_GAUGE, 'MB', _alloc('memory_mb')),
    'disk': (sample.TYPE_GAUGE, 'GB', _alloc('disk_gb')),
    'cpu.time': (sample.TYPE_CUMULATIVE, 'ns', _cpu_time),
    'memory.usage': (sample.TYPE_GAUGE, 'MB', _memory_usage),
    'network.incoming.bytes': (sample.TYPE_CUMULATIVE, 'B',
                               _network('rx_bytes')),
    'network.outgoing.bytes': (sample.TYPE_CUMULATIVE, 'B',
                               _network('tx_bytes')),
    'disk.read.bytes': (sample.TYPE_CUMULATIVE, 'B', _blkio('read_bytes')),
    'disk.write.bytes': (sample.TYPE_CUMULATIVE, 'B', _blkio('write_bytes')),
}

_METADATA_FIELDS = ('name', 'host', 'image', 'runtime', 'status',
                    'container_type', 'started_at', 'capsule_uuid',
                    'allocation_included_in_parent', 'allocation_basis')


def metadata(conf, entry):
    """What a resource is, beside what it used.

    Labels in the reserved namespaces (``metering.`` by default) arrive as
    ``user_metadata``, prefix stripped -- the same treatment, by the same
    code, as nova's instance metadata gets.
    """
    meta = {k: entry.get(k) for k in _METADATA_FIELDS if k in entry}
    if 'children' in entry:
        meta['children'] = entry['children']
    sample.add_reserved_user_metadata(conf, entry.get('labels') or {}, meta)
    return meta


class _ZunPollster(plugin_base.PollsterBase):
    KIND = None
    METER = None

    def __init__(self, conf):
        super().__init__(conf)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = zun_client.Client(self.conf)
        return self._client

    @property
    def default_discovery(self):
        return KINDS[self.KIND]['discovery']

    def _answer(self, manager, cache, resource):
        """The host's answer for this kind, read once per polling cycle."""
        key = ('zun', resource.host, resource.kind, resource.label)
        if key not in cache:
            cache[key] = self.client.host_stats(
                resource.host, [resource.kind], label=resource.label)
            discovery.heartbeat(manager, 'zun.read.%s.%s' % (
                resource.kind, resource.host))
        return cache[key]

    def get_samples(self, manager, cache, resources):
        kind = KINDS[self.KIND]
        sample_type, unit, volume_of = METERS[self.METER]
        name = '%s.%s' % (kind['prefix'], self.METER)
        for resource in resources or []:
            try:
                answer = self._answer(manager, cache, resource)
            except Exception as exc:
                # One host that cannot be read costs only its own figures;
                # the warning names it, since "no samples" alone cannot
                # tell an empty host from one that did not answer.
                LOG.warning('Could not read %(kind)s on Zun host %(host)s: '
                            '%(err)s', {'kind': self.KIND,
                                        'host': resource.host, 'err': exc})
                cache[('zun', resource.host, resource.kind,
                       resource.label)] = {'results': []}
                continue
            for entry in answer.get('results') or []:
                if entry.get('container_type') not in kind['types']:
                    continue
                volume = volume_of(entry)
                if volume is None:
                    continue
                yield sample.Sample(
                    name=name,
                    type=sample_type,
                    unit=unit,
                    volume=volume,
                    user_id=entry.get('user_id'),
                    project_id=entry.get('project_id'),
                    resource_id=entry['uuid'],
                    timestamp=(entry.get('reading') or {}).get('timestamp'),
                    resource_metadata=metadata(self.conf, entry))


def _pollster(kind, meter):
    class_name = '%s%sPollster' % (
        ''.join(p.capitalize() for p in kind.split('_')),
        ''.join(p.capitalize() for p in meter.replace('.', '_').split('_')))
    return type(class_name, (_ZunPollster,),
                {'KIND': kind, 'METER': meter,
                 '__doc__': 'Pollster for %s.%s.' % (
                     KINDS[kind]['prefix'], meter)})


for _kind in KINDS:
    for _meter in METERS:
        _cls = _pollster(_kind, _meter)
        globals()[_cls.__name__] = _cls
del _kind, _meter, _cls
