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
"""kubezoo's real nodes, to read billed pods from.

As with Zun, discovery hands out nodes rather than pods: kubezoo-metering
answers for a whole node in one call, so a node is the unit a polling agent
asks about and the unit workload partitioning shares out -- a resource is
partitioned by its string, which is the node's name.

Only real nodes are listed. A pod on a virtual node is a Zun capsule, metered
by the Zun pollsters as zun_capsule; kubezoo-metering leaves those out, so the
two never count one pod.
"""

from oslo_utils import timeutils

from ceilometer import kubezoo_client
from ceilometer.polling import plugin_base


class NodeToRead:
    """One node. Partitioned by its string, which is its name."""

    def __init__(self, node):
        self.node = node

    def __str__(self):
        return self.node

    def __repr__(self):
        return 'NodeToRead(%r)' % self.node

    def __eq__(self, other):
        return isinstance(other, NodeToRead) and self.node == other.node

    def __hash__(self):
        return hash(self.node)


def heartbeat(manager, *names):
    """Report these names through the agent's heartbeat, stamped now.

    The same three-way split as the Zun pollsters' (zun.hosts / zun.listed /
    zun.read), so the same alert rules apply:

    - `kubezoo.nodes`: the nodes were listed (even if there are none);
    - `kubezoo.listed.<node>`: the node was in that list;
    - `kubezoo.read.<node>`: its pods were read.

    A no-op unless the agent has a heartbeat (heartbeat_socket_dir).
    """
    send = getattr(manager, 'heartbeat', None)
    if send is None:
        return
    now = timeutils.utcnow().isoformat()
    for name in names:
        send(name, now)


class NodesDiscovery(plugin_base.DiscoveryBase):
    """kubezoo's real nodes."""

    def __init__(self, conf):
        super().__init__(conf)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = kubezoo_client.Client(self.conf)
        return self._client

    def discover(self, manager, param=None):
        # A deployment without kubezoo polls nothing and says nothing.
        if not self.conf.kubezoo.stats_endpoint:
            return []
        nodes = self.client.nodes()
        heartbeat(manager, 'kubezoo.nodes',
                  *('kubezoo.listed.%s' % n for n in nodes))
        return [NodeToRead(n) for n in nodes]
