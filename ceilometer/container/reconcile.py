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
"""End the Gnocchi resources of Zun objects that no longer exist.

A resource ends on Zun's delete event. A notification is sent once and
not retried: one lost on its way -- the bus down at the moment of the
delete -- leaves its resource open for ever (Gnocchi's
metric_inactive_after closes it later, at the time it noticed, not the
time the object went, and says nothing of why).

This compares what Gnocchi holds open with what Zun has now, and ends
what Zun no longer has at the last time it was seen -- its last measure,
or failing that when the resource started -- marking it
``ended_by=reconcile`` so it is never mistaken for an end an event gave.

A host that could not be read is left alone entirely: not being able to
ask is not an answer. A resource that started after the reading began,
less a margin, is left alone too: it may have been created after its host
was read.
"""

import datetime

from oslo_log import log
from oslo_utils import timeutils

LOG = log.getLogger(__name__)

#: The resource types reconciled, and the Zun kind each holds.
RESOURCE_TYPES = {'zun_container': 'container', 'zun_capsule': 'capsule'}

#: A resource younger than this, counted from the start of the reading, is
#: not ended: it may have been created after its host was read.
MARGIN = datetime.timedelta(minutes=10)

ENDED_BY = 'reconcile'


def _open_resources(gnocchi, resource_type, page=1000):
    marker = None
    while True:
        batch = gnocchi.resource.search(
            resource_type=resource_type,
            query={'=': {'ended_at': None}},
            limit=page, marker=marker)
        yield from batch
        if len(batch) < page:
            return
        marker = batch[-1]['id']


def _last_seen(gnocchi, resource):
    """The latest measure of any of its metrics, or when it started.

    Measures not yet aggregated count too (refresh): without them a dry
    run and the run after it could name different times, and the last
    point -- the one a lost delete leaves -- is the likeliest to be still
    waiting. Only resources already found gone are read, so it is cheap.
    """
    latest = None
    for metric_id in (resource.get('metrics') or {}).values():
        try:
            measures = gnocchi.metric.get_measures(metric_id, refresh=True)
        except Exception as exc:
            LOG.debug('no measures for %s: %s', metric_id, exc)
            continue
        if measures:
            stamp = measures[-1][0]
            stamp = (stamp if isinstance(stamp, datetime.datetime)
                     else timeutils.parse_isotime(str(stamp)))
            if latest is None or stamp > latest:
                latest = stamp
    if latest is None:
        return resource['started_at']
    return latest.isoformat()


def _present(zun, hosts):
    """{host: set of uuids} for each host that answered."""
    present = {}
    for host in hosts:
        try:
            answer = zun.host_stats(host, sorted(set(
                RESOURCE_TYPES.values())))
        except Exception as exc:
            LOG.warning('Zun host %(host)s could not be read; its resources '
                        'are left as they are: %(err)s',
                        {'host': host, 'err': exc})
            continue
        uuids = {e['uuid'] for e in answer.get('results') or []}
        uuids.update(e['uuid'] for e in answer.get('unavailable') or [])
        present[host] = uuids
    return present


def reconcile(zun, gnocchi, now=None, dry_run=False):
    """End what Gnocchi holds open and Zun no longer has.

    Answers [(resource_type, id, ended_at)] for each resource ended (or
    that would have been, with dry_run).
    """
    began = now or timeutils.utcnow(with_timezone=True)
    hosts = zun.hosts()
    present = _present(zun, hosts)
    cutoff = began - MARGIN
    ended = []
    for resource_type in RESOURCE_TYPES:
        for resource in _open_resources(gnocchi, resource_type):
            host = resource.get('host')
            if host in hosts and host not in present:
                continue        # its host did not answer
            if host in present and resource['id'] in present[host]:
                continue
            started = timeutils.parse_isotime(str(resource['started_at']))
            if started > cutoff:
                continue
            ended_at = _last_seen(gnocchi, resource)
            ended.append((resource_type, resource['id'], ended_at))
            LOG.info('%(type)s %(id)s is gone from Zun; ending it at '
                     '%(at)s, when it was last seen',
                     {'type': resource_type, 'id': resource['id'],
                      'at': ended_at})
            if not dry_run:
                gnocchi.resource.update(resource_type, resource['id'],
                                        {'ended_at': ended_at,
                                         'ended_by': ENDED_BY})
    return ended
