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
"""End the k8s_pod resources of pods that are no longer billed.

The same job as the Zun reconcile, for the same reason: a resource ends on
k8s.pod.delete.end, and a notification lost on its way leaves the resource
open for ever.

kubezoo-metering lists every tenant pod on a real node that still exists
(GET /v1/pods). Against that, an open resource whose pod is:

- listed and billed: left alone;
- listed but no longer billed (Succeeded or Failed): ended at the pod's own
  end time, which kubezoo-metering reports -- the same time delete.end
  would have carried;
- not listed: ended at the last time it was seen, its last measure or
  failing that its start, as the Zun reconcile does. Marked
  ``ended_by=reconcile`` so it is never mistaken for an end an event gave.

If the list cannot be read, nothing is ended: not being able to ask is not
an answer. kubezoo-metering refuses to answer before its caches are full for
the same reason -- an empty list would end everything. A resource that
started within MARGIN of the reading is left alone too.
"""

import datetime

from oslo_log import log
from oslo_utils import timeutils

from ceilometer.container.reconcile import _last_seen
from ceilometer.container.reconcile import _open_resources

LOG = log.getLogger(__name__)

RESOURCE_TYPE = 'k8s_pod'

#: A resource younger than this is not ended: it may have been created
#: after the list was read.
MARGIN = datetime.timedelta(minutes=10)

ENDED_BY = 'reconcile'


class Incomplete(Exception):
    """Some resources could not be ended; the rest were."""

    def __init__(self, ended, failed):
        self.ended = ended
        self.failed = failed
        super().__init__('%d k8s_pod resource(s) could not be ended: %s' % (
            len(failed), ', '.join(rid for _, rid, _ in failed)))


def _pod_uid(resource):
    # A pod UID is a UUID, so Gnocchi keeps it as the id; the original id is
    # what was sent, and is the safer of the two to compare.
    return resource.get('original_resource_id') or resource['id']


def reconcile(kubezoo, gnocchi, now=None, dry_run=False):
    """End what Gnocchi holds open and kubezoo no longer bills.

    Answers [(resource_type, id, ended_at)] for each resource ended (or
    that would have been, with dry_run).
    """
    began = now or timeutils.utcnow(with_timezone=True)
    pods = {p['pod_uid']: p for p in kubezoo.pods()}
    cutoff = began - MARGIN
    ended, failed = [], []
    for resource in _open_resources(gnocchi, RESOURCE_TYPE):
        pod = pods.get(_pod_uid(resource))
        if pod is not None and pod.get('billing'):
            continue
        started = timeutils.parse_isotime(str(resource['started_at']))
        if started > cutoff:
            continue
        if pod is not None and pod.get('ended_at'):
            # Never before the start: Gnocchi refuses that (400), and the
            # Zun reconcile learnt it the hard way (570d6e06).
            finished = timeutils.parse_isotime(pod['ended_at'])
            ended_at = max(finished, started).isoformat()
            why = 'it has finished'
            update = {'ended_at': ended_at}
        else:
            ended_at, why = _last_seen(gnocchi, resource), 'it is gone'
            update = {'ended_at': ended_at, 'ended_by': ENDED_BY}
        LOG.info('%(type)s %(id)s: %(why)s; ending it at %(at)s',
                 {'type': RESOURCE_TYPE, 'id': resource['id'], 'why': why,
                  'at': ended_at})
        if not dry_run:
            try:
                gnocchi.resource.update(RESOURCE_TYPE, resource['id'], update)
            except Exception as exc:
                LOG.error('%(type)s %(id)s could not be ended at %(at)s: '
                          '%(err)s', {'type': RESOURCE_TYPE,
                                      'id': resource['id'], 'at': ended_at,
                                      'err': exc})
                failed.append((RESOURCE_TYPE, resource['id'], ended_at))
                continue
        ended.append((RESOURCE_TYPE, resource['id'], ended_at))
    if failed:
        raise Incomplete(ended, failed)
    return ended
