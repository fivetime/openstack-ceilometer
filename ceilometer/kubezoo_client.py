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
"""A client for kubezoo-metering's stats API.

kubezoo tenants run pods on a shared Kubernetes cluster. Which of those pods
a tenant is billed for, which Keystone project it belongs to and how big it
is are decided by kubezoo-metering (github.com/fivetime/kubezoo-metering),
beside the kubezoo code that owns those rules; this asks it, the way the Zun
pollsters ask zun-api for a host's stats rather than reading the runtime.

Not a Keystone service: kubezoo-metering runs in the Kubernetes cluster that
hosts the control plane and is reached by URL with a bearer token.
"""

from urllib import parse

from oslo_config import cfg
import requests

OPTS = [
    cfg.StrOpt('stats_endpoint',
               help='Base URL of kubezoo-metering\'s stats API, e.g. '
                    'http://kubezoo-metering.kubezoo-system.svc:8090. '
                    'Unset, kubezoo pods are not polled.'),
    cfg.StrOpt('stats_token_file',
               help='File holding the bearer token the stats API requires.'),
    cfg.StrOpt('stats_ca_file',
               help='CA bundle to verify an https stats endpoint with.'),
    cfg.IntOpt('stats_timeout', default=10, min=1,
               help='Seconds to wait for one answer.'),
]


class Client:
    """Reads kubezoo's real nodes and the billed pods on them."""

    def __init__(self, conf):
        opts = conf.kubezoo
        if not opts.stats_endpoint:
            raise ValueError('[kubezoo] stats_endpoint is not set')
        self.endpoint = opts.stats_endpoint.rstrip('/')
        self.timeout = opts.stats_timeout
        self.session = requests.Session()
        if opts.stats_token_file:
            with open(opts.stats_token_file) as f:
                token = f.read().strip()
            self.session.headers['Authorization'] = 'Bearer ' + token
        if opts.stats_ca_file:
            self.session.verify = opts.stats_ca_file

    def _get(self, path):
        resp = self.session.get(self.endpoint + path, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def nodes(self):
        """The names of the real nodes -- not the capsule plane's."""
        return self._get('/v1/nodes').get('nodes') or []

    def node_pods(self, node):
        """One node's billed tenant pods, read now.

        Answers {'node', 'sampled_at', 'results': [...],
        'unavailable': [...]}.
        """
        return self._get('/v1/nodes/%s/pods' % parse.quote(node, safe=''))

    def pods(self):
        """Every tenant pod on a real node that still exists.

        Answers [{'pod_uid', 'billing', 'ended_at'?}].
        """
        return self._get('/v1/pods').get('pods') or []
